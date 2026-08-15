"""Config flow for Bluetooth HID Remote."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

import voluptuous as vol
from bleak.backends.bluezdbus.manager import get_global_bluez_manager
from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .compatibility import (
    CompatibilityRepairError,
    CompatibilityRepairUnsupportedError,
    async_install_ar_gatt_cache,
    is_ar_cache_repair_candidate,
)
from .const import (
    CONF_ADDRESS,
    CONF_KEY_PROFILE,
    CONF_NAME,
    CONF_VOICE_RESPONSE_PLAYER,
    DOMAIN,
    HID_SERVICE_UUID,
    KEY_PROFILE_ANDROID_TV,
    KEY_PROFILE_GOOGLE_TV,
    KEY_PROFILE_HID,
)
from .keymap import KeyMapError, async_load_custom_profiles
from .manager import bluez_device_path_from_address
from .pairing import (
    PairingDeviceNotFoundError,
    PairingError,
    PairingRejectedError,
    PairingServicePendingError,
    PairingStaleBondError,
    PairingTimeoutError,
    PairingVerificationError,
    async_pair_hogp_device,
    async_rebuild_hogp_bond,
    async_remove_hogp_device,
)

_LOGGER = logging.getLogger(__name__)

PairingOperation = Literal["pair", "replace", "repair", "reverify"]

_PAIRING_PROGRESS_ACTIONS: dict[PairingOperation, str] = {
    "pair": "pairing",
    "replace": "replacing_bond",
    "repair": "repairing_compatibility",
    "reverify": "verifying_service",
}

_PAIRING_FORM_STEPS: dict[PairingOperation, str] = {
    "pair": "bluetooth_confirm",
    "replace": "replace_bond",
    "repair": "compatibility_repair",
    "reverify": "service_pending",
}


def _supports_hogp(info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    """Return whether a discovery advertises the HOGP service."""
    return HID_SERVICE_UUID in {uuid.lower() for uuid in info.service_uuids}


class BluetoothHidRemoteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure a BLE HID-over-GATT remote."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovery: bluetooth.BluetoothServiceInfoBleak | None = None
        self._pairing_task: asyncio.Task[Any] | None = None
        self._pairing_operation: PairingOperation | None = None
        self._pairing_error: Exception | None = None
        self._compatibility_repair_applied = False

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle Bluetooth discovery."""
        if not _supports_hogp(discovery_info):
            return self.async_abort(reason="not_hogp")

        manager = await get_global_bluez_manager()
        if bluez_device_path_from_address(manager, discovery_info.address) is None:
            return self.async_abort(reason="not_direct")

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair and confirm a discovered remote."""
        if self._discovery is None:
            return self.async_abort(reason="device_not_found")
        if user_input is not None:
            return await self._async_start_pairing("pair")

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_replace_bond(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explicitly replace an unusable BlueZ bond for a reset remote."""
        if self._discovery is None:
            return self.async_abort(reason="device_not_found")
        if user_input is not None:
            return await self._async_start_pairing("replace")

        self._set_confirm_only()
        return self.async_show_form(
            step_id="replace_bond",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_service_pending(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wait for a complete bond to expose its delayed HOGP objects."""
        if self._discovery is None:
            return self.async_abort(reason="device_not_found")
        if user_input is not None:
            return await self._async_start_pairing("reverify")

        self._set_confirm_only()
        return self.async_show_form(
            step_id="service_pending",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_compatibility_repair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explicitly apply the tested HAOS workaround for an AR remote."""
        if self._discovery is None:
            return self.async_abort(reason="device_not_found")
        if user_input is not None:
            return await self._async_start_pairing("repair")

        self._set_confirm_only()
        return self.async_show_form(
            step_id="compatibility_repair",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_compatibility_wake(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wake the repaired remote before BlueZ reloads the seeded cache."""
        if self._discovery is None:
            return self.async_abort(reason="device_not_found")
        if user_input is not None:
            return await self._async_start_pairing("reverify")

        self._set_confirm_only()
        return self.async_show_form(
            step_id="compatibility_wake",
            description_placeholders=self.context["title_placeholders"],
        )

    async def _async_start_pairing(
        self, operation: PairingOperation
    ) -> ConfigFlowResult:
        """Start one pairing operation and immediately yield to the frontend."""
        assert self._discovery is not None
        if self._pairing_task is None:
            self._pairing_operation = operation
            self._pairing_error = None
            if operation == "replace":
                pairing_coro = async_pair_hogp_device(
                    self._discovery.address, replace_existing=True
                )
            elif operation == "repair":
                pairing_coro = async_install_ar_gatt_cache(
                    self.hass, self._discovery.address
                )
            else:
                pairing_coro = async_pair_hogp_device(self._discovery.address)
            self._pairing_task = self.hass.async_create_task(
                pairing_coro,
                name=f"Pair Bluetooth HID remote {self._discovery.address}",
                eager_start=False,
            )
        return await self.async_step_pairing_progress()

    async def async_step_pairing_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show pairing progress without holding the config-flow HTTP request."""
        if self._discovery is None:
            return self.async_abort(reason="device_not_found")
        task = self._pairing_task
        operation = self._pairing_operation
        if task is None or operation is None:
            _LOGGER.error("Bluetooth HID pairing progress resumed without a task")
            return self.async_abort(reason="device_not_found")

        if not task.done():
            return self.async_show_progress(
                step_id="pairing_progress",
                progress_action=_PAIRING_PROGRESS_ACTIONS[operation],
                description_placeholders=self.context["title_placeholders"],
                progress_task=task,
            )

        try:
            task.result()
        except (CompatibilityRepairError, PairingError) as err:
            self._pairing_error = err
        except Exception as err:  # pragma: no cover - defensive integration guard
            _LOGGER.exception("Unexpected Bluetooth HID pairing failure")
            self._pairing_error = err
        finally:
            self._pairing_task = None

        return self.async_show_progress_done(next_step_id="pairing_result")

    async def async_step_pairing_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Route a completed pairing operation to its explicit result."""
        if self._discovery is None:
            return self.async_abort(reason="device_not_found")
        operation = self._pairing_operation
        error = self._pairing_error
        self._pairing_operation = None
        self._pairing_error = None

        if operation is None:
            _LOGGER.error("Bluetooth HID pairing result resumed without an operation")
            return self.async_abort(reason="device_not_found")
        if error is None and operation == "repair":
            self._compatibility_repair_applied = True
            return await self.async_step_compatibility_wake()
        if error is None:
            return self.async_create_entry(
                title=self._discovery.name,
                data={
                    CONF_ADDRESS: self._discovery.address,
                    CONF_KEY_PROFILE: KEY_PROFILE_ANDROID_TV,
                    CONF_NAME: self._discovery.name,
                },
            )

        if isinstance(error, PairingServicePendingError):
            if await self._async_supports_compatibility_repair():
                if self._compatibility_repair_applied:
                    return self._show_pairing_form(
                        "compatibility_wake", "service_pending_after_repair"
                    )
                return await self.async_step_compatibility_repair()
            if operation == "reverify":
                return await self.async_step_replace_bond()
            return await self.async_step_service_pending()
        if isinstance(error, PairingStaleBondError) and operation != "replace":
            return await self.async_step_replace_bond()

        error_key = self._pairing_error_key(error)
        self._log_pairing_error(error, error_key)
        form_step = _PAIRING_FORM_STEPS[operation]
        if operation == "reverify" and self._compatibility_repair_applied:
            form_step = "compatibility_wake"
        return self._show_pairing_form(form_step, error_key)

    @staticmethod
    def _pairing_error_key(error: Exception) -> str:
        """Map pairing exceptions to translated config-flow errors."""
        if isinstance(error, PairingDeviceNotFoundError):
            return "device_not_found"
        if isinstance(error, PairingTimeoutError):
            return "pairing_timeout"
        if isinstance(error, PairingRejectedError):
            return "pairing_rejected"
        if isinstance(error, PairingVerificationError):
            return "pairing_verification_failed"
        if isinstance(error, CompatibilityRepairUnsupportedError):
            return "compatibility_repair_unsupported"
        if isinstance(error, CompatibilityRepairError):
            return "compatibility_repair_failed"
        return "pairing_failed"

    async def _async_supports_compatibility_repair(self) -> bool:
        """Return whether the bonded target exactly matches the tested AR."""
        assert self._discovery is not None
        manager = await get_global_bluez_manager()
        device_path = bluez_device_path_from_address(manager, self._discovery.address)
        return device_path is not None and is_ar_cache_repair_candidate(
            manager, device_path
        )

    @staticmethod
    def _log_pairing_error(error: Exception, error_key: str) -> None:
        """Log one concise pairing failure after the progress task completes."""
        _LOGGER.warning("Bluetooth HID pairing failed (%s): %s", error_key, error)

    def _show_pairing_form(self, step_id: str, error: str) -> ConfigFlowResult:
        """Return to the originating confirmation form with a visible error."""
        self._set_confirm_only()
        return self.async_show_form(
            step_id=step_id,
            description_placeholders=self.context["title_placeholders"],
            errors={"base": error},
        )

    async def _async_direct_discoveries(
        self,
    ) -> dict[str, bluetooth.BluetoothServiceInfoBleak]:
        """Return visible HOGP devices backed by HAOS's local BlueZ adapter."""
        manager = await get_global_bluez_manager()
        return {
            info.address: info
            for info in bluetooth.async_discovered_service_info(self.hass, True)
            if _supports_hogp(info)
            and bluez_device_path_from_address(manager, info.address) is not None
        }

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a currently visible, directly attached BLE HOGP remote."""
        discoveries = await self._async_direct_discoveries()
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery = discoveries.get(address)
            if discovery is None:
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._selection_schema(discoveries),
                    errors={"base": "device_not_found"},
                )
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            self._discovery = discovery
            self.context["title_placeholders"] = {"name": discovery.name}
            return await self.async_step_bluetooth_confirm()

        if not discoveries:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user", data_schema=self._selection_schema(discoveries)
        )

    @staticmethod
    def _selection_schema(
        discoveries: dict[str, bluetooth.BluetoothServiceInfoBleak],
    ) -> vol.Schema:
        labels = {
            address: f"{info.name} ({address})" for address, info in discoveries.items()
        }
        return vol.Schema({vol.Required(CONF_ADDRESS): vol.In(labels)})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the key-profile options flow."""
        return BluetoothHidRemoteOptionsFlow()


class BluetoothHidRemoteOptionsFlow(OptionsFlow):
    """Configure key mapping and confirmed bond recovery."""

    def __init__(self) -> None:
        """Initialize an options flow without touching its config entry yet."""
        self._bond_task: asyncio.Task[Any] | None = None
        self._bond_error: Exception | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a safe configuration or recovery operation."""
        return self.async_show_menu(
            step_id="init", menu_options=["key_profile", "voice", "rebuild_bond"]
        )

    async def async_step_key_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure the key mapping profile."""
        errors: dict[str, str] = {}
        try:
            custom_profiles = await async_load_custom_profiles(self.hass)
        except KeyMapError:
            custom_profiles = {}
            errors["base"] = "invalid_keymap"

        labels = {
            KEY_PROFILE_HID: "HID",
            KEY_PROFILE_ANDROID_TV: "Android TV / Fire TV",
            KEY_PROFILE_GOOGLE_TV: "Google TV",
            **{name: f"Custom: {name}" for name in sorted(custom_profiles)},
        }
        available_profiles = set(labels)
        current = self.config_entry.options.get(
            CONF_KEY_PROFILE,
            self.config_entry.data.get(CONF_KEY_PROFILE, KEY_PROFILE_HID),
        )
        if current not in labels:
            labels[current] = f"Unavailable: {current}"

        if user_input is not None and not errors:
            profile = user_input[CONF_KEY_PROFILE]
            if profile not in available_profiles:
                errors["base"] = "invalid_profile"
            else:
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_KEY_PROFILE: profile}
                )

        return self.async_show_form(
            step_id="key_profile",
            data_schema=vol.Schema(
                {vol.Required(CONF_KEY_PROFILE, default=current): vol.In(labels)}
            ),
            errors=errors,
        )

    async def async_step_voice(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure optional TTS response playback for remote Assist."""
        current = self.config_entry.options.get(CONF_VOICE_RESPONSE_PLAYER, "")
        if user_input is not None:
            options = dict(self.config_entry.options)
            response_player = user_input.get(CONF_VOICE_RESPONSE_PLAYER)
            if response_player:
                options[CONF_VOICE_RESPONSE_PLAYER] = response_player
            else:
                options.pop(CONF_VOICE_RESPONSE_PLAYER, None)
            return self.async_create_entry(data=options)

        response_field = vol.Optional(CONF_VOICE_RESPONSE_PLAYER)
        if current:
            response_field = vol.Optional(CONF_VOICE_RESPONSE_PLAYER, default=current)
        return self.async_show_form(
            step_id="voice",
            data_schema=vol.Schema(
                {
                    response_field: selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="media_player")
                    )
                }
            ),
        )

    async def async_step_rebuild_bond(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm removal and recreation of only this entry's BlueZ bond."""
        if user_input is not None:
            if self._bond_task is None:
                self._bond_error = None
                self._bond_task = self.hass.async_create_task(
                    self._async_rebuild_entry_bond(),
                    name=(
                        "Rebuild Bluetooth HID bond "
                        f"{self.config_entry.data[CONF_ADDRESS]}"
                    ),
                    eager_start=False,
                )
            return await self.async_step_rebuild_progress()

        return self.async_show_form(
            step_id="rebuild_bond",
            description_placeholders={
                "name": self.config_entry.data.get(CONF_NAME, "BLE HID Remote")
            },
        )

    async def async_step_rebuild_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show progress while the selected bond is rebuilt."""
        task = self._bond_task
        if task is None:
            return self.async_abort(reason="device_not_found")
        if not task.done():
            return self.async_show_progress(
                step_id="rebuild_progress",
                progress_action="rebuilding_bond",
                description_placeholders={
                    "name": self.config_entry.data.get(CONF_NAME, "BLE HID Remote")
                },
                progress_task=task,
            )

        try:
            task.result()
        except Exception as err:  # rendered on the confirmed recovery form
            self._bond_error = err
        finally:
            self._bond_task = None
        return self.async_show_progress_done(next_step_id="rebuild_result")

    async def async_step_rebuild_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish or return a recoverable bond-rebuild error."""
        error = self._bond_error
        self._bond_error = None
        if error is None:
            return self.async_abort(reason="bond_rebuilt")

        _LOGGER.warning("Bluetooth HID bond rebuild failed: %s", error)
        return self.async_show_form(
            step_id="rebuild_bond",
            description_placeholders={
                "name": self.config_entry.data.get(CONF_NAME, "BLE HID Remote")
            },
            errors={"base": BluetoothHidRemoteConfigFlow._pairing_error_key(error)},
        )

    async def _async_rebuild_entry_bond(self) -> None:
        """Fail closed while replacing one loaded entry's bond."""
        entry = self.config_entry
        address = entry.data[CONF_ADDRESS]
        if not await self.hass.config_entries.async_unload(entry.entry_id):
            raise PairingError(
                "Home Assistant could not unload the remote before bond removal"
            )

        rebuilt = False
        try:
            await async_rebuild_hogp_bond(address)
            rebuilt = True
            if not await self.hass.config_entries.async_setup(entry.entry_id):
                raise PairingError(
                    "Home Assistant could not restore protected remote input"
                )
        except Exception:
            if rebuilt:
                try:
                    await async_remove_hogp_device(address, ignore_missing=True)
                except PairingError:
                    _LOGGER.exception(
                        "Could not remove newly rebuilt bond after setup failure"
                    )
            raise
