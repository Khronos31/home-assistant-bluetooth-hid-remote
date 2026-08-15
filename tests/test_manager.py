"""Tests for passive BlueZ HOGP report helpers."""

import asyncio
from types import SimpleNamespace

import pytest
from dbus_fast.constants import MessageType

from custom_components.bluetooth_hid_remote.const import (
    EVENT_KEY_PRESSED,
    EVENT_KEY_RELEASED,
    HID_REPORT_MAP_UUID,
    HID_REPORT_REFERENCE_UUID,
    HID_REPORT_UUID,
)
from custom_components.bluetooth_hid_remote.hid import HidReportDecoder, HidUsage
from custom_components.bluetooth_hid_remote.manager import (
    BluetoothHidRemoteManager,
    HidInputReport,
    characteristic_handle_from_path,
    event_type_for_report,
    parse_report_reference,
)


def test_bluez_connection_only_updates_passive_state() -> None:
    """A BlueZ connection is observed without queuing a client connection."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    manager.connected = False
    manager.input_report_count = 3
    manager._report_metadata_by_path = {"path": (1, 2)}
    manager._active_usages_by_path = {}
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


@pytest.mark.asyncio
async def test_hid_metadata_is_loaded_from_existing_bluez_connection() -> None:
    """Report Map and Reference reads populate the decoder and report ID."""
    report_map_path = "/service004f/char0055"
    report_path = "/service004f/char005d"
    reference_path = f"{report_path}/desc0060"
    keyboard_map = bytes.fromhex("0507850195037508150025ff190029ff8100")

    class Bus:
        connected = True

        async def call(self, message):
            values = {
                report_map_path: keyboard_map,
                reference_path: bytes([1, 1]),
            }
            return SimpleNamespace(
                message_type=MessageType.METHOD_RETURN,
                error_name=None,
                body=[values[message.path]],
            )

    report_map_characteristic = SimpleNamespace(
        uuid=HID_REPORT_MAP_UUID,
        obj=(report_map_path, {}),
        descriptors=[],
    )
    report_characteristic = SimpleNamespace(
        uuid=HID_REPORT_UUID,
        obj=(report_path, {}),
        handle=0x5D,
        descriptors=[
            SimpleNamespace(
                uuid=HID_REPORT_REFERENCE_UUID,
                obj=(reference_path, {}),
            )
        ],
    )
    service = SimpleNamespace(
        characteristics=[report_map_characteristic, report_characteristic],
        get_characteristic=lambda uuid: (
            report_map_characteristic if uuid == HID_REPORT_MAP_UUID else None
        ),
    )
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    manager.connected = True
    manager._stopping = False
    manager._bluez_manager = SimpleNamespace(_bus=Bus())
    manager._report_decoder = None
    manager.report_map = None
    metadata = {report_path: (0, 0x5D)}

    await manager._async_load_hid_metadata(service, metadata)

    assert manager.report_map == keyboard_map
    assert metadata == {report_path: (1, 0x5D)}
    assert manager._report_decoder.decode(1, bytes.fromhex("580000")) == (
        HidUsage(0x07, 0x58, "Keypad Enter"),
    )


def test_unmapped_bluez_value_is_published_with_path_handle() -> None:
    """A report is not lost while BlueZ service metadata is unavailable."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    manager._report_metadata_by_path = {}
    manager._report_decoder = None
    manager._active_usages_by_path = {}
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    path = "/org/bluez/hci0/dev_00_11_22_33_44_55/service0010/char0012"
    manager._async_bluez_value_changed(path, bytes.fromhex("510000"))
    assert received == [HidInputReport(0, 0x12, bytes.fromhex("510000"))]


def test_decoded_usage_is_carried_from_press_to_release() -> None:
    """A zero release report keeps the key identity from the active press."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char005d"
    manager._report_metadata_by_path = {path: (1, 0x5D)}
    manager._report_decoder = HidReportDecoder.from_report_map(
        bytes.fromhex("0507850195037508150025ff190029ff8100")
    )
    manager._active_usages_by_path = {}
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    manager._async_bluez_value_changed(path, bytes.fromhex("580000"))
    manager._async_bluez_value_changed(path, bytes.fromhex("000000"))

    usage = HidUsage(0x07, 0x58, "Keypad Enter")
    assert received == [
        HidInputReport(1, 0x5D, bytes.fromhex("580000"), (usage,)),
        HidInputReport(1, 0x5D, bytes.fromhex("000000"), (usage,)),
    ]
    assert manager._active_usages_by_path == {}


def test_undecodable_nonzero_report_clears_stale_active_usage() -> None:
    """An unknown press cannot attach an earlier key to a later release."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char005d"
    manager._report_metadata_by_path = {path: (2, 0x5D)}
    manager._report_decoder = HidReportDecoder.from_report_map(
        bytes.fromhex("0507850195037508150025ff190029ff8100")
    )
    stale = HidUsage(0x07, 0x58, "Keypad Enter")
    manager._active_usages_by_path = {path: (stale,)}
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    manager._async_bluez_value_changed(path, bytes.fromhex("010000"))
    manager._async_bluez_value_changed(path, bytes.fromhex("000000"))

    assert received == [
        HidInputReport(2, 0x5D, bytes.fromhex("010000")),
        HidInputReport(2, 0x5D, bytes.fromhex("000000")),
    ]
    assert manager._active_usages_by_path == {}


def test_characteristic_handle_from_path() -> None:
    """BlueZ uses the GATT handle as the hexadecimal path suffix."""
    assert characteristic_handle_from_path("/service0010/char001a") == 0x1A
    assert characteristic_handle_from_path("/service0010/charnope") is None
    assert characteristic_handle_from_path("/org/bluez/hci0/dev_x") is None


def test_report_reference() -> None:
    """A Report Reference contains an ID and report type."""
    assert parse_report_reference(bytes([7, 1])) == (7, 1)
    assert parse_report_reference(b"") is None
    assert parse_report_reference(bytes([1, 2, 3])) is None


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
