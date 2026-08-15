"""Tests for BlueZ-owned HID remote pairing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bleak.backends.bluezdbus import defs
from dbus_fast.constants import MessageType
from dbus_fast.errors import DBusError
from dbus_fast.message import Message

from custom_components.bluetooth_hid_remote.const import HID_SERVICE_UUID
from custom_components.bluetooth_hid_remote.pairing import (
    BluetoothHidPairingAgent,
    PairingDeviceNotFoundError,
    PairingRejectedError,
    PairingServicePendingError,
    PairingStaleBondError,
    PairingTimeoutError,
    async_pair_hogp_device,
    async_rebuild_hogp_bond,
    async_remove_hogp_device,
)

ADDRESS = "88:34:37:C9:CA:71"
ADAPTER_PATH = "/org/bluez/hci0"
DEVICE_PATH = f"{ADAPTER_PATH}/dev_{ADDRESS.replace(':', '_')}"
SERVICE_PATH = f"{DEVICE_PATH}/service0010"


def _manager(
    *, paired: bool = False, preferred_bearer: str | None = None
) -> SimpleNamespace:
    """Return a minimal BlueZ manager with one direct HID remote."""
    device_properties = {
        "Address": ADDRESS,
        "Adapter": ADAPTER_PATH,
        "Paired": paired,
        "Bonded": paired,
        "ServicesResolved": paired,
        "Trusted": paired,
        "UUIDs": [HID_SERVICE_UUID],
    }
    if preferred_bearer is not None:
        device_properties["PreferredBearer"] = preferred_bearer
    return SimpleNamespace(
        _properties={
            DEVICE_PATH: {defs.DEVICE_INTERFACE: device_properties},
            SERVICE_PATH: {
                defs.GATT_SERVICE_INTERFACE: {
                    "Device": DEVICE_PATH,
                    "UUID": HID_SERVICE_UUID,
                }
            },
        },
        _service_map={DEVICE_PATH: {SERVICE_PATH}},
    )


def _success_reply() -> SimpleNamespace:
    """Return a successful D-Bus method reply."""
    return SimpleNamespace(message_type=MessageType.METHOD_RETURN)


class FakeBus:
    """Record the D-Bus operations made by the pairing helper."""

    def __init__(
        self,
        manager: SimpleNamespace,
        *,
        pairing_succeeds: bool = True,
        restore_service_on_pair: bool = False,
        pairing_error_name: str = "org.bluez.Error.AuthenticationFailed",
        pairing_error_details: str = "rejected",
    ):
        self.manager = manager
        self.pairing_succeeds = pairing_succeeds
        self.restore_service_on_pair = restore_service_on_pair
        self.pairing_error_name = pairing_error_name
        self.pairing_error_details = pairing_error_details
        self.calls: list[Message] = []
        self.exports: list[tuple[str, object]] = []
        self.unexports: list[tuple[str, object | None]] = []
        self.disconnected = False

    def export(self, path: str, interface: object) -> None:
        self.exports.append((path, interface))

    def unexport(self, path: str, interface: object | None = None) -> None:
        self.unexports.append((path, interface))

    def disconnect(self) -> None:
        self.disconnected = True

    async def call(self, message: Message) -> Message | SimpleNamespace:
        self.calls.append(message)
        if message.member == "Pair":
            if not self.pairing_succeeds:
                return SimpleNamespace(
                    message_type=MessageType.ERROR,
                    error_name=self.pairing_error_name,
                    body=[self.pairing_error_details],
                )
            device = self.manager._properties[DEVICE_PATH][defs.DEVICE_INTERFACE]
            device.update(
                {
                    "Paired": True,
                    "Bonded": True,
                    "ServicesResolved": True,
                }
            )
            if self.restore_service_on_pair:
                self.manager._service_map[DEVICE_PATH].add(SERVICE_PATH)
        elif message.member == "RemoveDevice":
            device = self.manager._properties[DEVICE_PATH][defs.DEVICE_INTERFACE]
            device.update(
                {
                    "Paired": False,
                    "Bonded": False,
                    "ServicesResolved": False,
                    "Trusted": False,
                }
            )
        return _success_reply()


def test_pairing_agent_is_bound_to_one_device_and_hid_service() -> None:
    """The temporary agent must not authorize unrelated BlueZ requests."""
    agent = BluetoothHidPairingAgent(DEVICE_PATH)

    agent.RequestConfirmation(DEVICE_PATH, 123456)
    agent.RequestAuthorization(DEVICE_PATH)
    agent.AuthorizeService(DEVICE_PATH, HID_SERVICE_UUID.upper())

    with pytest.raises(DBusError):
        agent.RequestConfirmation("/org/bluez/hci0/dev_OTHER", 123456)
    with pytest.raises(DBusError):
        agent.AuthorizeService(DEVICE_PATH, "0000180f-0000-1000-8000-00805f9b34fb")
    with pytest.raises(DBusError):
        agent.RequestPinCode(DEVICE_PATH)
    with pytest.raises(DBusError):
        agent.RequestPasskey(DEVICE_PATH)


@pytest.mark.asyncio
async def test_pairing_registers_agent_verifies_hogp_and_trusts() -> None:
    """A fresh device is paired, verified, then trusted on a dedicated bus."""
    manager = _manager()
    bus = FakeBus(manager)
    bus_factory = AsyncMock(return_value=bus)

    result = await async_pair_hogp_device(
        ADDRESS, manager=manager, bus_factory=bus_factory
    )

    assert result.device_path == DEVICE_PATH
    assert result.already_paired is False
    assert [call.member for call in bus.calls] == [
        "StartDiscovery",
        "RegisterAgent",
        "RequestDefaultAgent",
        "Pair",
        "Set",
        "UnregisterAgent",
        "StopDiscovery",
    ]
    assert bus.calls[4].body[:2] == [defs.DEVICE_INTERFACE, "Trusted"]
    assert bus.exports and bus.unexports
    assert bus.disconnected is True


@pytest.mark.asyncio
async def test_dual_mode_remote_is_forced_to_le_before_pairing() -> None:
    """A BLE-only integration must not let BlueZ prefer the Classic bearer."""
    manager = _manager(preferred_bearer="last-used")
    bus = FakeBus(manager)

    await async_pair_hogp_device(
        ADDRESS, manager=manager, bus_factory=AsyncMock(return_value=bus)
    )

    assert [call.member for call in bus.calls] == [
        "StartDiscovery",
        "Set",
        "RegisterAgent",
        "RequestDefaultAgent",
        "Pair",
        "Set",
        "UnregisterAgent",
        "StopDiscovery",
    ]
    assert bus.calls[1].body[:2] == [defs.DEVICE_INTERFACE, "PreferredBearer"]
    assert bus.calls[1].body[2].value == "le"


@pytest.mark.asyncio
async def test_prepaired_hogp_device_does_not_open_pairing_bus() -> None:
    """An existing valid bond is preserved and reused without Agent1."""
    manager = _manager(paired=True)
    bus_factory = AsyncMock()

    result = await async_pair_hogp_device(
        ADDRESS, manager=manager, bus_factory=bus_factory
    )

    assert result.already_paired is True
    bus_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_new_pairing_removes_only_flow_created_device() -> None:
    """A failed fresh pairing is removed while the temporary agent is cleaned up."""
    manager = _manager()
    bus = FakeBus(manager, pairing_succeeds=False)

    with pytest.raises(PairingRejectedError):
        await async_pair_hogp_device(
            ADDRESS, manager=manager, bus_factory=AsyncMock(return_value=bus)
        )

    assert [call.member for call in bus.calls] == [
        "StartDiscovery",
        "RegisterAgent",
        "RequestDefaultAgent",
        "Pair",
        "RemoveDevice",
        "UnregisterAgent",
        "StopDiscovery",
    ]
    assert bus.disconnected is True


@pytest.mark.asyncio
async def test_page_timeout_is_reported_as_pairing_timeout() -> None:
    """A bearer connection page timeout is not mislabeled as HOGP verification."""
    manager = _manager(preferred_bearer="last-used")
    bus = FakeBus(
        manager,
        pairing_succeeds=False,
        pairing_error_name="org.bluez.Error.ConnectionAttemptFailed",
        pairing_error_details="Page Timeout",
    )

    with pytest.raises(PairingTimeoutError):
        await async_pair_hogp_device(
            ADDRESS, manager=manager, bus_factory=AsyncMock(return_value=bus)
        )

    assert [call.member for call in bus.calls] == [
        "StartDiscovery",
        "Set",
        "RegisterAgent",
        "RequestDefaultAgent",
        "Pair",
        "RemoveDevice",
        "UnregisterAgent",
        "StopDiscovery",
    ]


@pytest.mark.asyncio
async def test_authentication_timeout_is_reported_as_pairing_timeout() -> None:
    """An authentication timeout must not be mislabeled as HOGP verification."""
    manager = _manager()
    bus = FakeBus(
        manager,
        pairing_succeeds=False,
        pairing_error_name="org.bluez.Error.AuthenticationTimeout",
        pairing_error_details="Authentication Timeout",
    )

    with pytest.raises(PairingTimeoutError):
        await async_pair_hogp_device(
            ADDRESS, manager=manager, bus_factory=AsyncMock(return_value=bus)
        )

    assert [call.member for call in bus.calls] == [
        "StartDiscovery",
        "RegisterAgent",
        "RequestDefaultAgent",
        "Pair",
        "RemoveDevice",
        "UnregisterAgent",
        "StopDiscovery",
    ]


@pytest.mark.asyncio
async def test_complete_bond_without_gatt_service_is_preserved_as_pending() -> None:
    """A complete bond is retained while concrete HOGP objects are delayed."""
    manager = _manager()
    manager._service_map[DEVICE_PATH].clear()
    bus = FakeBus(manager)

    with pytest.raises(PairingServicePendingError):
        await async_pair_hogp_device(
            ADDRESS,
            manager=manager,
            bus_factory=AsyncMock(return_value=bus),
            timeout=0.02,
        )

    assert [call.member for call in bus.calls] == [
        "StartDiscovery",
        "RegisterAgent",
        "RequestDefaultAgent",
        "Pair",
        "UnregisterAgent",
        "StopDiscovery",
    ]


@pytest.mark.asyncio
async def test_stale_existing_bond_requires_explicit_replacement() -> None:
    """A reset remote's old BlueZ bond is never deleted without confirmation."""
    manager = _manager(paired=True)
    manager._service_map[DEVICE_PATH].clear()
    manager._properties[DEVICE_PATH][defs.DEVICE_INTERFACE]["ServicesResolved"] = False
    bus_factory = AsyncMock()

    with pytest.raises(PairingStaleBondError):
        await async_pair_hogp_device(ADDRESS, manager=manager, bus_factory=bus_factory)

    bus_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_replacement_removes_only_stale_target_then_repairs() -> None:
    """Confirmed replacement removes the stale target and creates a fresh bond."""
    manager = _manager(paired=True)
    manager._service_map[DEVICE_PATH].clear()
    manager._properties[DEVICE_PATH][defs.DEVICE_INTERFACE]["ServicesResolved"] = False
    remove_bus = FakeBus(manager)
    pair_bus = FakeBus(manager, restore_service_on_pair=True)
    bus_factory = AsyncMock(side_effect=[remove_bus, pair_bus])

    result = await async_pair_hogp_device(
        ADDRESS,
        manager=manager,
        bus_factory=bus_factory,
        replace_existing=True,
    )

    assert result.already_paired is False
    assert [call.member for call in remove_bus.calls] == [
        "StartDiscovery",
        "RemoveDevice",
        "StopDiscovery",
    ]
    assert [call.member for call in pair_bus.calls] == [
        "StartDiscovery",
        "RegisterAgent",
        "RequestDefaultAgent",
        "Pair",
        "Set",
        "UnregisterAgent",
        "StopDiscovery",
    ]


@pytest.mark.asyncio
async def test_explicit_remove_targets_only_selected_direct_device() -> None:
    """The public unpair helper is an idempotent, address-scoped operation."""
    manager = _manager(paired=True)
    bus = FakeBus(manager)

    assert (
        await async_remove_hogp_device(
            ADDRESS, manager=manager, bus_factory=AsyncMock(return_value=bus)
        )
        is True
    )

    assert [call.member for call in bus.calls] == ["RemoveDevice"]
    assert bus.calls[0].path == ADAPTER_PATH
    assert bus.calls[0].body == [DEVICE_PATH]
    assert bus.disconnected is True


@pytest.mark.asyncio
async def test_remove_can_ignore_an_already_absent_target() -> None:
    """Config-entry cleanup may safely repeat after BlueZ forgot the device."""
    manager = SimpleNamespace(_properties={})
    bus_factory = AsyncMock()

    assert (
        await async_remove_hogp_device(
            ADDRESS,
            manager=manager,
            bus_factory=bus_factory,
            ignore_missing=True,
        )
        is False
    )
    bus_factory.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_rebuild_replaces_even_a_valid_bond() -> None:
    """Recovery explicitly removes a valid bond before pairing it again."""
    manager = _manager(paired=True)
    remove_bus = FakeBus(manager)
    pair_bus = FakeBus(manager)
    bus_factory = AsyncMock(side_effect=[remove_bus, pair_bus])

    result = await async_rebuild_hogp_bond(
        ADDRESS, manager=manager, bus_factory=bus_factory
    )

    assert result.already_paired is False
    assert [call.member for call in remove_bus.calls] == [
        "StartDiscovery",
        "RemoveDevice",
        "StopDiscovery",
    ]
    assert [call.member for call in pair_bus.calls] == [
        "StartDiscovery",
        "RegisterAgent",
        "RequestDefaultAgent",
        "Pair",
        "Set",
        "UnregisterAgent",
        "StopDiscovery",
    ]


@pytest.mark.asyncio
async def test_proxy_only_device_is_rejected_before_pairing() -> None:
    """Pairing requires a device object on HAOS's direct BlueZ adapter."""
    manager = SimpleNamespace(_properties={})
    bus_factory = AsyncMock()

    with pytest.raises(PairingDeviceNotFoundError):
        await async_pair_hogp_device(ADDRESS, manager=manager, bus_factory=bus_factory)

    bus_factory.assert_not_awaited()
