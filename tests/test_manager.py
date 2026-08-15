"""Tests for passive BlueZ HOGP report helpers."""

import asyncio
from types import SimpleNamespace

import pytest
from dbus_fast.constants import MessageType

from custom_components.bluetooth_hid_remote.const import (
    EVENT_KEY_PRESSED,
    EVENT_KEY_RELEASED,
)
from custom_components.bluetooth_hid_remote.manager import (
    BluetoothHidRemoteManager,
    HidInputReport,
    characteristic_handle_from_path,
    event_type_for_report,
)


def test_bluez_connection_only_updates_passive_state() -> None:
    """A BlueZ connection is observed without queuing a client connection."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    manager.connected = False
    manager.input_report_count = 3
    manager._report_metadata_by_path = {"path": (1, 2)}
    manager._notification_paths = {"path"}
    manager._stopping = False

    class Entry:
        def async_create_background_task(self, _hass, coro, **_kwargs) -> None:
            coro.close()

    manager.entry = Entry()
    manager.hass = object()

    manager._async_bluez_connected_changed(False)
    assert not manager.connected
    assert manager.input_report_count == 0
    assert manager._report_metadata_by_path == {}
    assert manager._notification_paths == set()

    manager._async_bluez_connected_changed(True)
    assert manager.connected


@pytest.mark.asyncio
async def test_notifications_use_existing_bluez_bus_and_are_released() -> None:
    """Notification ownership does not create or disconnect a BLE client."""

    class Bus:
        connected = True

        def __init__(self) -> None:
            self.calls = []

        async def call(self, message):
            self.calls.append(message)
            return SimpleNamespace(
                message_type=MessageType.METHOD_RETURN,
                error_name=None,
            )

    bus = Bus()
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    manager.connected = True
    manager.connection_failures = 0
    manager._bluez_manager = SimpleNamespace(_bus=bus)
    manager._notification_lock = asyncio.Lock()
    manager._notification_paths = set()
    manager._stopping = False

    paths = ["/service0010/char0012", "/service0010/char0014"]
    await manager._async_start_notifications(paths)
    await manager._async_start_notifications(paths)

    assert [message.member for message in bus.calls] == [
        "StartNotify",
        "StartNotify",
    ]
    assert manager._notification_paths == set(paths)

    await manager._async_stop_notifications()
    assert [message.member for message in bus.calls] == [
        "StartNotify",
        "StartNotify",
        "StopNotify",
        "StopNotify",
    ]
    assert manager._notification_paths == set()


@pytest.mark.asyncio
async def test_late_subscription_is_released_while_stopping() -> None:
    """An unload racing StartNotify does not leak its D-Bus subscription."""

    manager = object.__new__(BluetoothHidRemoteManager)

    class Bus:
        connected = True

        def __init__(self) -> None:
            self.calls = []

        async def call(self, message):
            self.calls.append(message)
            if message.member == "StartNotify":
                manager._stopping = True
            return SimpleNamespace(
                message_type=MessageType.METHOD_RETURN,
                error_name=None,
            )

    bus = Bus()
    manager.address = "00:11:22:33:44:55"
    manager.connected = True
    manager.connection_failures = 0
    manager._bluez_manager = SimpleNamespace(_bus=bus)
    manager._notification_lock = asyncio.Lock()
    manager._notification_paths = set()
    manager._stopping = False

    await manager._async_start_notifications(["/service0010/char0012"])

    assert [message.member for message in bus.calls] == [
        "StartNotify",
        "StopNotify",
    ]
    assert manager._notification_paths == set()


def test_unmapped_bluez_value_is_published_with_path_handle() -> None:
    """A report is not lost while BlueZ service metadata is unavailable."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    manager._report_metadata_by_path = {}
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    path = "/org/bluez/hci0/dev_00_11_22_33_44_55/service0010/char0012"
    manager._async_bluez_value_changed(path, bytes.fromhex("510000"))
    assert received == [HidInputReport(0, 0x12, bytes.fromhex("510000"))]


def test_characteristic_handle_from_path() -> None:
    """BlueZ uses the GATT handle as the hexadecimal path suffix."""
    assert characteristic_handle_from_path("/service0010/char001a") == 0x1A
    assert characteristic_handle_from_path("/service0010/charnope") is None
    assert characteristic_handle_from_path("/org/bluez/hci0/dev_x") is None


def test_report_event_type() -> None:
    """Nonzero input is a press and all-zero input is a release."""
    assert event_type_for_report(bytes.fromhex("510000")) == EVENT_KEY_PRESSED
    assert event_type_for_report(bytes.fromhex("000000")) == EVENT_KEY_RELEASED


def test_input_report_properties() -> None:
    """The immutable report object exposes the spike classification."""
    report = HidInputReport(1, 94, bytes.fromhex("510000"))
    assert report.report_id == 1
    assert report.characteristic_handle == 94
    assert report.event_type == EVENT_KEY_PRESSED
