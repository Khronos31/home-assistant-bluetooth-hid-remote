"""Pair Bluetooth HID remotes through BlueZ without owning the HID connection."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.manager import BlueZManager, get_global_bluez_manager
from bleak.backends.bluezdbus.utils import (
    assert_reply,
    get_dbus_authenticator,
)
from bleak.exc import BleakDBusError
from dbus_fast.aio import MessageBus
from dbus_fast.constants import BusType
from dbus_fast.errors import DBusError
from dbus_fast.message import Message
from dbus_fast.service import ServiceInterface, method
from dbus_fast.signature import Variant

from .const import HID_SERVICE_UUID
from .manager import bluez_device_path_from_address

_LOGGER = logging.getLogger(__name__)

AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
AGENT_MANAGER_PATH = "/org/bluez"
AGENT_CAPABILITY = "DisplayYesNo"
PAIRING_TIMEOUT = 60.0
SERVICE_DISCOVERY_TIMEOUT = 10.0

_BLUEZ_REJECTED_ERRORS = {
    "org.bluez.Error.AuthenticationCanceled",
    "org.bluez.Error.AuthenticationFailed",
    "org.bluez.Error.AuthenticationRejected",
    "org.bluez.Error.Rejected",
}

# dbus-fast reads these annotation names as raw D-Bus signatures. Defining the
# names also lets static tooling understand the otherwise domain-specific types.
o = str
q = int
s = str
u = int


class PairingError(Exception):
    """Base error for a BlueZ HID pairing attempt."""


class PairingDeviceNotFoundError(PairingError):
    """The remote is not visible on HAOS's direct Bluetooth adapter."""


class PairingRejectedError(PairingError):
    """BlueZ or the remote rejected authentication."""


class PairingTimeoutError(PairingError):
    """Pairing did not complete before the deadline."""


class PairingVerificationError(PairingError):
    """Pairing returned without a complete HOGP bond."""


class PairingStaleBondError(PairingVerificationError):
    """BlueZ retains a bond that the reset remote can no longer use."""


class PairingServicePendingError(PairingError):
    """The bond is complete, but BlueZ has not exported HOGP objects yet."""


@dataclass(frozen=True, slots=True)
class PairingResult:
    """Verified BlueZ pairing details."""

    device_path: str
    already_paired: bool


def _reject(message: str) -> DBusError:
    return DBusError("org.bluez.Error.Rejected", message)


class BluetoothHidPairingAgent(ServiceInterface):
    """Temporary Agent1 accepting only one explicitly selected HOGP device."""

    def __init__(self, device_path: str) -> None:
        super().__init__(AGENT_INTERFACE)
        self._device_path = device_path

    def _require_target(self, device: str) -> None:
        if device != self._device_path:
            raise _reject("Pairing request is for an unexpected Bluetooth device")

    @method()
    def Release(self):
        """Acknowledge release by BlueZ."""

    @method()
    def RequestPinCode(self, device: o) -> s:
        """Reject legacy PIN entry, which is not supported by this flow."""
        self._require_target(device)
        _LOGGER.debug("BlueZ requested PIN-code entry for pairing target")
        raise _reject("PIN-code pairing is not supported")

    @method()
    def DisplayPinCode(self, device: o, pincode: s):
        """Accept a display-only notification for the selected device."""
        self._require_target(device)

    @method()
    def RequestPasskey(self, device: o) -> u:
        """Reject passkey entry, which a remote cannot complete through HA UI."""
        self._require_target(device)
        _LOGGER.debug("BlueZ requested passkey entry for pairing target")
        raise _reject("Passkey-entry pairing is not supported")

    @method()
    def DisplayPasskey(self, device: o, passkey: u, entered: q):
        """Accept a display-only passkey notification for the selected device."""
        self._require_target(device)

    @method()
    def RequestConfirmation(self, device: o, passkey: u):
        """Confirm the exact device selected on the preceding HA form."""
        self._require_target(device)
        _LOGGER.debug("Confirmed BlueZ pairing request for selected HID device")

    @method()
    def RequestAuthorization(self, device: o):
        """Authorize only the exact selected device."""
        self._require_target(device)
        _LOGGER.debug("Authorized BlueZ pairing request for selected HID device")

    @method()
    def AuthorizeService(self, device: o, uuid: s):
        """Authorize only the HID-over-GATT profile on the selected device."""
        self._require_target(device)
        _LOGGER.debug("BlueZ requested pairing authorization for service %s", uuid)
        if uuid.casefold() != HID_SERVICE_UUID:
            raise _reject(f"Unexpected Bluetooth service {uuid}")

    @method()
    def Cancel(self):
        """Acknowledge cancellation by BlueZ."""


BusFactory = Callable[[], Awaitable[MessageBus]]


async def _async_open_system_bus() -> MessageBus:
    """Open a dedicated system bus connection for one pairing attempt."""
    return await MessageBus(
        bus_type=BusType.SYSTEM,
        auth=get_dbus_authenticator(),
    ).connect()


def _device_properties(manager: BlueZManager, device_path: str) -> dict[str, Any]:
    try:
        return manager._properties[device_path][defs.DEVICE_INTERFACE]
    except KeyError as err:
        raise PairingDeviceNotFoundError(
            "The selected remote is no longer visible on the direct adapter"
        ) from err


def _has_hogp(properties: dict[str, Any]) -> bool:
    return HID_SERVICE_UUID in {
        str(uuid).casefold() for uuid in properties.get("UUIDs", ())
    }


def _has_hogp_service(manager: BlueZManager, device_path: str) -> bool:
    """Return whether BlueZ exported a real HOGP GATT service object."""
    for service_path in manager._service_map.get(device_path, ()):
        service = manager._properties.get(service_path, {}).get(
            defs.GATT_SERVICE_INTERFACE, {}
        )
        if str(service.get("UUID", "")).casefold() == HID_SERVICE_UUID:
            return True
    return False


def _verify_existing_bond(
    manager: BlueZManager, device_path: str, properties: dict[str, Any]
) -> None:
    if not properties.get("Paired") or not properties.get("Bonded"):
        raise PairingVerificationError("BlueZ did not create a persistent bond")
    if not _has_hogp(properties):
        raise PairingVerificationError(
            "The paired device does not expose the HID-over-GATT service"
        )
    if not properties.get("ServicesResolved"):
        raise PairingVerificationError("BlueZ has not resolved the bonded services")
    if not _has_hogp_service(manager, device_path):
        raise PairingServicePendingError(
            "BlueZ did not export the HID-over-GATT service objects"
        )


async def _async_wait_for_property(
    manager: BlueZManager, device_path: str, property_name: str
) -> None:
    """Observe a BlueZ property becoming true, including transient states."""
    if _device_properties(manager, device_path).get(property_name):
        return

    wait_condition = getattr(manager, "_wait_condition", None)
    if wait_condition is not None:
        await wait_condition(device_path, property_name, True)
        return

    while not _device_properties(manager, device_path).get(property_name):
        await asyncio.sleep(0.01)


async def _async_wait_for_fresh_pair(
    manager: BlueZManager, device_path: str, timeout: float
) -> None:
    """Observe bond and service resolution from before Device1.Pair begins."""
    async with asyncio.timeout(timeout):
        await asyncio.gather(
            _async_wait_for_property(manager, device_path, "Paired"),
            _async_wait_for_property(manager, device_path, "Bonded"),
            _async_wait_for_property(manager, device_path, "ServicesResolved"),
        )


async def _async_wait_for_hogp_service(
    manager: BlueZManager, device_path: str, timeout: float
) -> None:
    """Allow BlueZ a bounded grace period to export resolved GATT objects."""
    async with asyncio.timeout(timeout):
        while not _has_hogp_service(manager, device_path):
            await asyncio.sleep(0.1)


async def _async_call(bus: MessageBus, message: Message) -> None:
    reply = await bus.call(message)
    assert_reply(reply)


async def _async_set_trusted(bus: MessageBus, device_path: str) -> None:
    await _async_call(
        bus,
        Message(
            destination=defs.BLUEZ_SERVICE,
            path=device_path,
            interface=defs.PROPERTIES_INTERFACE,
            member="Set",
            signature="ssv",
            body=[defs.DEVICE_INTERFACE, "Trusted", Variant("b", True)],
        ),
    )


async def _async_set_preferred_le_bearer(bus: MessageBus, device_path: str) -> None:
    """Force a dual-mode device's next connection attempt onto BLE."""
    await _async_call(
        bus,
        Message(
            destination=defs.BLUEZ_SERVICE,
            path=device_path,
            interface=defs.PROPERTIES_INTERFACE,
            member="Set",
            signature="ssv",
            body=[defs.DEVICE_INTERFACE, "PreferredBearer", Variant("s", "le")],
        ),
    )


async def _async_start_discovery(bus: MessageBus, adapter_path: str) -> None:
    """Keep one discovery client active for the complete pairing exchange."""
    await _async_call(
        bus,
        Message(
            destination=defs.BLUEZ_SERVICE,
            path=adapter_path,
            interface=defs.ADAPTER_INTERFACE,
            member="StartDiscovery",
        ),
    )


async def _async_stop_discovery(bus: MessageBus, adapter_path: str) -> None:
    """Release only the discovery request owned by this D-Bus connection."""
    await _async_call(
        bus,
        Message(
            destination=defs.BLUEZ_SERVICE,
            path=adapter_path,
            interface=defs.ADAPTER_INTERFACE,
            member="StopDiscovery",
        ),
    )


async def _async_remove_device(
    bus: MessageBus, adapter_path: str, device_path: str
) -> None:
    await _async_call(
        bus,
        Message(
            destination=defs.BLUEZ_SERVICE,
            path=adapter_path,
            interface=defs.ADAPTER_INTERFACE,
            member="RemoveDevice",
            signature="o",
            body=[device_path],
        ),
    )


async def _async_wait_for_direct_device(
    manager: BlueZManager, address: str, timeout: float
) -> str:
    """Wait for a removed pairing-mode device to be discovered again."""
    async with asyncio.timeout(timeout):
        while True:
            device_path = bluez_device_path_from_address(manager, address)
            if device_path is not None:
                properties = _device_properties(manager, device_path)
                if not properties.get("Paired") and not properties.get("Bonded"):
                    break
            await asyncio.sleep(0.1)
    return device_path


async def async_remove_hogp_device(
    address: str,
    *,
    manager: BlueZManager | None = None,
    bus_factory: BusFactory = _async_open_system_bus,
    ignore_missing: bool = False,
) -> bool:
    """Remove exactly one direct-adapter Bluetooth device from BlueZ."""
    if manager is None:
        manager = await get_global_bluez_manager()

    device_path = bluez_device_path_from_address(manager, address)
    if device_path is None:
        if ignore_missing:
            return False
        raise PairingDeviceNotFoundError(
            "The selected remote is no longer visible on the direct adapter"
        )

    properties = _device_properties(manager, device_path)
    adapter_path = properties.get("Adapter")
    if not isinstance(adapter_path, str):
        raise PairingVerificationError("BlueZ did not report the owning adapter")

    try:
        bus = await bus_factory()
    except Exception as err:
        raise PairingError("Could not open the BlueZ system bus") from err
    try:
        try:
            await _async_remove_device(bus, adapter_path, device_path)
        except BleakDBusError as err:
            raise PairingError(
                f"BlueZ could not remove the selected remote: {err}"
            ) from err
    finally:
        bus.disconnect()
    return True


async def async_rebuild_hogp_bond(
    address: str,
    *,
    manager: BlueZManager | None = None,
    bus_factory: BusFactory = _async_open_system_bus,
    timeout: float = PAIRING_TIMEOUT,
) -> PairingResult:
    """Remove, rediscover, pair, and verify one selected HOGP remote."""
    if manager is None:
        manager = await get_global_bluez_manager()

    device_path = bluez_device_path_from_address(manager, address)
    if device_path is None:
        raise PairingDeviceNotFoundError(
            "The selected remote is no longer visible on the direct adapter"
        )

    properties = _device_properties(manager, device_path)
    adapter_path = properties.get("Adapter")
    if not isinstance(adapter_path, str):
        raise PairingVerificationError("BlueZ did not report the owning adapter")

    try:
        bus = await bus_factory()
    except Exception as err:
        raise PairingError("Could not open the BlueZ system bus") from err
    discovery_started = False
    try:
        try:
            await _async_start_discovery(bus, adapter_path)
        except BleakDBusError as err:
            raise PairingError(
                f"BlueZ could not start discovery before rebuilding the bond: {err}"
            ) from err
        discovery_started = True

        try:
            await _async_remove_device(bus, adapter_path, device_path)
        except BleakDBusError as err:
            raise PairingError(
                f"BlueZ could not remove the selected bond: {err}"
            ) from err

        try:
            await _async_wait_for_direct_device(manager, address, timeout)
        except TimeoutError as err:
            raise PairingTimeoutError(
                "The remote was not rediscovered after removing its bond"
            ) from err
    finally:
        if discovery_started:
            with suppress(Exception):
                await _async_stop_discovery(bus, adapter_path)
        bus.disconnect()

    return await async_pair_hogp_device(
        address,
        manager=manager,
        bus_factory=bus_factory,
        timeout=timeout,
    )


async def async_pair_hogp_device(
    address: str,
    *,
    manager: BlueZManager | None = None,
    bus_factory: BusFactory = _async_open_system_bus,
    timeout: float = PAIRING_TIMEOUT,
    replace_existing: bool = False,
) -> PairingResult:
    """Pair and verify one direct-adapter HOGP remote through BlueZ."""
    if manager is None:
        manager = await get_global_bluez_manager()

    device_path = bluez_device_path_from_address(manager, address)
    if device_path is None:
        raise PairingDeviceNotFoundError(
            "The selected remote is not visible on HAOS's direct Bluetooth adapter"
        )

    initial_properties = _device_properties(manager, device_path)
    initially_paired = bool(
        initial_properties.get("Paired") or initial_properties.get("Bonded")
    )
    if initially_paired:
        replacement_error: PairingError | None = None
        try:
            _verify_existing_bond(manager, device_path, initial_properties)
        except PairingServicePendingError as err:
            if replace_existing:
                replacement_error = err
            else:
                try:
                    await _async_wait_for_hogp_service(
                        manager,
                        device_path,
                        min(timeout, SERVICE_DISCOVERY_TIMEOUT),
                    )
                except TimeoutError as wait_err:
                    raise err from wait_err
                _verify_existing_bond(
                    manager, device_path, _device_properties(manager, device_path)
                )
        except PairingVerificationError as err:
            if not replace_existing:
                raise PairingStaleBondError(
                    "BlueZ has an existing bond without usable HOGP services"
                ) from err
            replacement_error = err

        if replacement_error is not None:
            return await async_rebuild_hogp_bond(
                address,
                manager=manager,
                bus_factory=bus_factory,
                timeout=timeout,
            )

        if initial_properties.get("Trusted"):
            return PairingResult(device_path=device_path, already_paired=True)

        try:
            bus = await bus_factory()
        except Exception as err:
            raise PairingError("Could not open the BlueZ system bus") from err
        try:
            try:
                await _async_set_trusted(bus, device_path)
            except BleakDBusError as err:
                raise PairingError(
                    f"BlueZ could not trust the paired remote: {err}"
                ) from err
        finally:
            bus.disconnect()
        return PairingResult(device_path=device_path, already_paired=True)

    adapter_path = initial_properties.get("Adapter")
    if not isinstance(adapter_path, str):
        raise PairingVerificationError("BlueZ did not report the owning adapter")

    try:
        bus = await bus_factory()
    except Exception as err:
        raise PairingError("Could not open the BlueZ system bus") from err
    agent_path = "/org/homeassistant/bluetooth_hid_remote/agent_" + secrets.token_hex(8)
    agent = BluetoothHidPairingAgent(device_path)
    agent_registered = False
    discovery_started = False
    pair_started = False
    pair_complete = False
    error: PairingError | None = None

    bus.export(agent_path, agent)
    try:
        try:
            await _async_start_discovery(bus, adapter_path)
        except BleakDBusError as err:
            raise PairingError(
                f"BlueZ could not start discovery for pairing: {err}"
            ) from err
        discovery_started = True

        if initial_properties.get("PreferredBearer") not in (None, "le"):
            try:
                await _async_set_preferred_le_bearer(bus, device_path)
            except BleakDBusError as err:
                raise PairingError(
                    f"BlueZ could not select the LE bearer: {err}"
                ) from err

        try:
            await _async_call(
                bus,
                Message(
                    destination=defs.BLUEZ_SERVICE,
                    path=AGENT_MANAGER_PATH,
                    interface=AGENT_MANAGER_INTERFACE,
                    member="RegisterAgent",
                    signature="os",
                    body=[agent_path, AGENT_CAPABILITY],
                ),
            )
        except BleakDBusError as err:
            raise PairingError(
                f"BlueZ could not register a pairing agent: {err}"
            ) from err
        agent_registered = True
        try:
            await _async_call(
                bus,
                Message(
                    destination=defs.BLUEZ_SERVICE,
                    path=AGENT_MANAGER_PATH,
                    interface=AGENT_MANAGER_INTERFACE,
                    member="RequestDefaultAgent",
                    signature="o",
                    body=[agent_path],
                ),
            )
        except BleakDBusError as err:
            raise PairingError(
                f"BlueZ could not make the pairing agent active: {err}"
            ) from err
        pair_started = True
        verification_task = asyncio.create_task(
            _async_wait_for_fresh_pair(manager, device_path, timeout)
        )
        try:
            async with asyncio.timeout(timeout):
                await _async_call(
                    bus,
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=device_path,
                        interface=defs.DEVICE_INTERFACE,
                        member="Pair",
                    ),
                )
                await verification_task
        except TimeoutError as err:
            properties = _device_properties(manager, device_path)
            error = PairingTimeoutError(
                "Bluetooth pairing timed out "
                f"(paired={bool(properties.get('Paired'))}, "
                f"bonded={bool(properties.get('Bonded'))}, "
                "services_resolved="
                f"{bool(properties.get('ServicesResolved'))}, "
                f"hogp_service={_has_hogp_service(manager, device_path)})"
            )
            error.__cause__ = err
        except BleakDBusError as err:
            if err.dbus_error in _BLUEZ_REJECTED_ERRORS:
                error = PairingRejectedError("Bluetooth pairing was rejected")
            elif err.dbus_error == "org.bluez.Error.AuthenticationTimeout":
                error = PairingTimeoutError(
                    "BlueZ timed out while authenticating the remote"
                )
            elif (
                err.dbus_error == "org.bluez.Error.ConnectionAttemptFailed"
                and err.dbus_error_details == "Page Timeout"
            ):
                error = PairingTimeoutError(
                    "BlueZ timed out while connecting to the remote"
                )
            else:
                error = PairingVerificationError(f"BlueZ pairing failed: {err}")
            error.__cause__ = err
        finally:
            if not verification_task.done():
                verification_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await verification_task

        if error is None:
            try:
                with suppress(TimeoutError):
                    await _async_wait_for_hogp_service(
                        manager,
                        device_path,
                        min(timeout, SERVICE_DISCOVERY_TIMEOUT),
                    )
                _verify_existing_bond(
                    manager, device_path, _device_properties(manager, device_path)
                )
                await _async_set_trusted(bus, device_path)
            except PairingServicePendingError as err:
                # Pairing, bonding, and service resolution are complete. Keep
                # this selected device's bond so a physical wake/reconnection
                # can finish BlueZ GATT export from the recovery flow.
                pair_complete = True
                error = err
            except PairingError as err:
                error = err
            except BleakDBusError as err:
                error = PairingVerificationError(
                    f"BlueZ could not trust the paired remote: {err}"
                )
                error.__cause__ = err

        if error is not None:
            raise error
        pair_complete = True
        return PairingResult(device_path=device_path, already_paired=False)
    finally:
        if pair_started and not pair_complete:
            try:
                await _async_remove_device(bus, adapter_path, device_path)
            except Exception:
                _LOGGER.warning(
                    "Could not remove incomplete Bluetooth pairing for %s",
                    address,
                    exc_info=True,
                )
        if agent_registered:
            try:
                await _async_call(
                    bus,
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=AGENT_MANAGER_PATH,
                        interface=AGENT_MANAGER_INTERFACE,
                        member="UnregisterAgent",
                        signature="o",
                        body=[agent_path],
                    ),
                )
            except Exception:
                _LOGGER.debug(
                    "Could not unregister temporary pairing agent", exc_info=True
                )
        if discovery_started:
            try:
                await _async_stop_discovery(bus, adapter_path)
            except Exception:
                _LOGGER.debug(
                    "Could not stop temporary pairing discovery", exc_info=True
                )
        bus.unexport(agent_path, agent)
        bus.disconnect()
