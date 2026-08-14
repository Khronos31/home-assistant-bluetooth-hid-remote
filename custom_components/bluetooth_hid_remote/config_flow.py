"""Config flow for Bluetooth HID Remote."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_ADDRESS, CONF_NAME, DOMAIN, HID_SERVICE_UUID


def _supports_hogp(info: bluetooth.BluetoothServiceInfoBleak) -> bool:
    """Return whether a discovery advertises the HOGP service."""
    return HID_SERVICE_UUID in {uuid.lower() for uuid in info.service_uuids}


class BluetoothHidRemoteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure a BLE HID-over-GATT remote."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovery: bluetooth.BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle Bluetooth discovery."""
        if not _supports_hogp(discovery_info):
            return self.async_abort(reason="not_hogp")

        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered, already-paired remote for the spike."""
        if self._discovery is None:
            return self.async_abort(reason="device_not_found")
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovery.name,
                data={
                    CONF_ADDRESS: self._discovery.address,
                    CONF_NAME: self._discovery.name,
                },
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a currently visible BLE HOGP remote."""
        discoveries = {
            info.address: info
            for info in bluetooth.async_discovered_service_info(self.hass, True)
            if _supports_hogp(info)
        }
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
