"""Tests for passive BlueZ HOGP report helpers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bleak.backends.bluezdbus import defs
from dbus_fast.constants import MessageType

from custom_components.bluetooth_hid_remote.const import (
    ATVV_CONTROL_UUID,
    ATVV_RX_UUID,
    ATVV_TX_UUID,
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
    bluez_device_path_from_address,
    characteristic_handle_from_path,
    event_type_for_report,
    parse_report_reference,
)
from custom_components.bluetooth_hid_remote.voice import (
    HidVoicePacket,
    PcmVoicePacket,
    VoicePacket,
)


@pytest.mark.asyncio
async def test_manager_start_failure_releases_input_grab() -> None:
    """A partial setup cannot leave the console keyboard exclusively owned."""
    entry = SimpleNamespace(
        data={"address": "00:11:22:33:44:55", "name": "Remote"},
        async_create_background_task=lambda *_args, **_kwargs: None,
    )
    manager = BluetoothHidRemoteManager(object(), entry, SimpleNamespace())
    manager.input_grabber = SimpleNamespace(
        async_start=AsyncMock(), async_stop=AsyncMock()
    )
    manager._async_register_bluez_watcher = AsyncMock(side_effect=RuntimeError)

    with pytest.raises(RuntimeError):
        await manager.async_start()

    manager.input_grabber.async_start.assert_awaited_once_with()
    manager.input_grabber.async_stop.assert_awaited_once_with()


def test_bluez_path_falls_back_to_the_direct_adapter_by_address() -> None:
    """A proxy-preferred HA device cannot hide the local BlueZ device path."""
    expected_path = "/org/bluez/hci1/dev_88_34_37_C9_CA_71"
    manager = SimpleNamespace(
        _properties={
            "/org/bluez/hci0": {defs.ADAPTER_INTERFACE: {"Address": "local"}},
            expected_path: {defs.DEVICE_INTERFACE: {"Address": "88:34:37:C9:CA:71"}},
        }
    )

    assert bluez_device_path_from_address(manager, "88:34:37:c9:ca:71") == expected_path
    assert bluez_device_path_from_address(manager, "00:00:00:00:00:00") is None


def test_bluez_connection_only_updates_passive_state() -> None:
    """A BlueZ connection is observed without queuing a client connection."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    manager.connected = False
    manager.input_report_count = 3
    manager._report_metadata_by_path = {"path": (1, 2)}
    manager._atvv_paths = {"atvv": "control"}
    manager._atvv_close_timer = None
    manager._ignored_value_paths = {"metadata"}
    manager._initial_cached_values_by_path = {"path": b"cached"}
    manager._active_usages_by_path = {}
    manager._active_report_paths = {"path"}
    manager._notification_paths = {"path"}
    manager._voice_listeners = set()
    manager.last_voice_packet = None
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
    assert manager._atvv_paths == {}
    assert manager._ignored_value_paths == set()
    assert manager._initial_cached_values_by_path == {}
    assert manager._active_report_paths == set()
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
    paths = ["/service0010/char0012", "/service0010/char0014"]
    manager._bluez_manager = SimpleNamespace(
        _bus=bus,
        _properties={
            paths[0]: {
                defs.GATT_CHARACTERISTIC_INTERFACE: {
                    "Value": bytearray.fromhex("f10000")
                }
            }
        },
    )
    manager._notification_lock = asyncio.Lock()
    manager._notification_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._stopping = False

    await manager._async_start_notifications(paths)
    await manager._async_start_notifications(paths)

    assert [message.member for message in bus.calls] == [
        "StartNotify",
        "StartNotify",
    ]
    assert manager._notification_paths == set(paths)
    assert manager._initial_cached_values_by_path == {paths[0]: bytes.fromhex("f10000")}

    await manager._async_stop_notifications()
    assert [message.member for message in bus.calls] == [
        "StartNotify",
        "StartNotify",
        "StopNotify",
        "StopNotify",
    ]
    assert manager._notification_paths == set()
    assert manager._initial_cached_values_by_path == {}


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
    manager._bluez_manager = SimpleNamespace(_bus=bus, _properties={})
    manager._notification_lock = asyncio.Lock()
    manager._notification_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._stopping = False

    await manager._async_start_notifications(["/service0010/char0012"])

    assert [message.member for message in bus.calls] == [
        "StartNotify",
        "StopNotify",
    ]
    assert manager._notification_paths == set()


@pytest.mark.asyncio
async def test_atvv_command_uses_exact_bluez_write_value_payload() -> None:
    """Voice negotiation writes only the documented command to ATVV TX."""

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
    manager._stopping = False
    manager._atvv_tx_path = "/service0010/char0020"
    manager._bluez_manager = SimpleNamespace(_bus=bus)

    await manager._async_write_atvv(bytes.fromhex("0a0100000300"), "GET_CAPS")

    message = bus.calls[0]
    assert message.member == "WriteValue"
    assert message.signature == "aya{sv}"
    assert message.body[0] == bytes.fromhex("0a0100000300")
    assert message.body[1]["type"].value == "command"


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
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._report_decoder = None
    manager.report_map = None
    metadata = {report_path: (0, 0x5D)}

    await manager._async_load_hid_metadata(service, metadata)

    assert manager.report_map == keyboard_map
    assert metadata == {report_path: (1, 0x5D)}
    assert manager._report_decoder.decode(1, bytes.fromhex("580000")) == (
        HidUsage(0x07, 0x58, "Keypad ENTER"),
    )


def test_unmapped_bluez_value_is_published_with_path_handle() -> None:
    """A report is not lost while BlueZ service metadata is unavailable."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    manager._report_metadata_by_path = {}
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._report_decoder = None
    manager._active_usages_by_path = {}
    manager._active_report_paths = set()
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    path = "/org/bluez/hci0/dev_00_11_22_33_44_55/service0010/char0012"
    manager._async_bluez_value_changed(path, bytes.fromhex("510000"))
    assert received == [HidInputReport(0, 0x12, bytes.fromhex("510000"))]


def test_descriptor_backed_opus_report_is_not_published_as_a_key() -> None:
    """Voice payloads bypass the event entity and last-key sensor listeners."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char0065"
    manager._report_metadata_by_path = {path: (0xF0, 0x65)}
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._report_decoder = HidReportDecoder.from_report_map(
        bytes.fromhex("06ff000900a10185f095507508150025ff8100c0")
    )
    manager._active_usages_by_path = {}
    manager._active_report_paths = set()
    manager.last_report = None
    manager.last_voice_packet = None
    key_reports: list[HidInputReport] = []
    voice_reports: list[HidVoicePacket | None] = []
    manager._listeners = {key_reports.append}
    manager._voice_listeners = {voice_reports.append}
    packet = bytes.fromhex(
        "b826965bd777c885ede76ed1cbf0a21ca1a70f985f4214c83a8a5e3879646572"
        "7f2b41b7956304fdbec738919d791e442ad2607dc892efdc0277ee609142f8b37"
        "5cb35cc2d284384cb272ed13fee3451"
    )

    manager._async_bluez_value_changed(path, packet)

    assert key_reports == []
    assert manager.last_report is None
    assert voice_reports == [HidVoicePacket(0xF0, 0x65, packet)]


def test_atvv_characteristics_are_mapped_without_tx_subscription() -> None:
    """The passive probe subscribes only to Google control and audio paths."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    tx = SimpleNamespace(
        uuid=ATVV_TX_UUID,
        handle=0x20,
        properties=["write"],
        obj=("/service0010/char0020", {}),
    )
    audio = SimpleNamespace(
        uuid=ATVV_RX_UUID,
        handle=0x22,
        properties=["notify"],
        obj=("/service0010/char0022", {}),
    )
    control = SimpleNamespace(
        uuid=ATVV_CONTROL_UUID,
        handle=0x24,
        properties=["notify"],
        obj=("/service0010/char0024", {}),
    )

    paths = manager._map_atvv_characteristics(
        SimpleNamespace(characteristics=[control, tx, audio])
    )

    assert paths == ["/service0010/char0022", "/service0010/char0024"]
    assert manager._atvv_tx_path == "/service0010/char0020"
    assert manager._atvv_paths == {
        "/service0010/char0022": "audio",
        "/service0010/char0024": "control",
    }


def test_atvv_values_never_enter_key_pipeline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ATVV audio becomes dedicated PCM without entering key/log pipelines."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    control_path = "/service0010/char0024"
    audio_path = "/service0010/char0022"
    manager._atvv_paths = {control_path: "control", audio_path: "audio"}
    manager._atvv_audio_packet_count = 0
    manager._atvv_decoder = None
    manager._atvv_frame_bytes = 200
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._report_metadata_by_path = {}
    manager._listeners = {pytest.fail}
    manager._voice_listeners = set()
    manager.last_voice_packet = None
    voice_packets: list[VoicePacket | None] = []
    manager._voice_listeners = {voice_packets.append}

    with caplog.at_level("DEBUG"):
        manager._async_bluez_value_changed(control_path, bytes.fromhex("04000200"))
        manager._async_bluez_value_changed(audio_path, bytes.fromhex("deadbeef"))

    assert "opcode=0x04 bytes=4" in caplog.text
    assert "ATVV audio address=" in caplog.text
    assert "deadbeef" not in caplog.text
    assert manager._atvv_audio_packet_count == 1
    assert voice_packets == [
        PcmVoicePacket(
            16_000,
            bytes.fromhex("f5ffe2ffd6ffbdffa5ff7dff35ff9ffe"),
        )
    ]


def test_report_id_alone_cannot_hide_a_normal_key_report() -> None:
    """An F0 report without the descriptor and Opus signature stays visible."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char0065"
    manager._report_metadata_by_path = {path: (0xF0, 0x65)}
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._report_decoder = HidReportDecoder.from_report_map(
        bytes.fromhex("06ff000900a10185f095037508150025ff8100c0")
    )
    manager._active_usages_by_path = {}
    manager._active_report_paths = set()
    manager.last_report = None
    manager.last_voice_packet = None
    key_reports: list[HidInputReport] = []
    manager._listeners = {key_reports.append}
    manager._voice_listeners = set()

    manager._async_bluez_value_changed(path, bytes.fromhex("b80000"))

    assert key_reports == [
        HidInputReport(
            0xF0,
            0x65,
            bytes.fromhex("b80000"),
            (HidUsage(0xFF, 0xB8),),
        )
    ]


def test_decoded_usage_is_carried_from_press_to_release() -> None:
    """A zero release report keeps the key identity from the active press."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char005d"
    manager._report_metadata_by_path = {path: (1, 0x5D)}
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._report_decoder = HidReportDecoder.from_report_map(
        bytes.fromhex("0507850195037508150025ff190029ff8100")
    )
    manager._active_usages_by_path = {}
    manager._active_report_paths = set()
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    manager._async_bluez_value_changed(path, bytes.fromhex("580000"))
    manager._async_bluez_value_changed(path, bytes.fromhex("000000"))

    usage = HidUsage(0x07, 0x58, "Keypad ENTER")
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
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._report_decoder = HidReportDecoder.from_report_map(
        bytes.fromhex("0507850195037508150025ff190029ff8100")
    )
    stale = HidUsage(0x07, 0x58, "Keypad ENTER")
    manager._active_usages_by_path = {path: (stale,)}
    manager._active_report_paths = set()
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


def test_static_report_map_value_is_not_published_as_a_key() -> None:
    """A Report Map read observed by BlueZ cannot create a false press event."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char0055"
    manager._ignored_value_paths = {path}
    manager._initial_cached_values_by_path = {}
    manager._report_metadata_by_path = {}
    manager._report_decoder = None
    manager._active_usages_by_path = {}
    manager._active_report_paths = set()
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    manager._async_bluez_value_changed(path, bytes.fromhex("05010906a101"))

    assert received == []
    assert manager.last_report is None


def test_idle_release_without_a_preceding_press_is_not_published() -> None:
    """StartNotify's initial zero value cannot create a false release event."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char005d"
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {}
    manager._report_metadata_by_path = {path: (1, 0x5D)}
    manager._report_decoder = None
    manager._active_usages_by_path = {}
    manager._active_report_paths = set()
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    manager._async_bluez_value_changed(path, bytes.fromhex("000000"))

    assert received == []
    assert manager.last_report is None


def test_initial_cached_press_and_its_idle_release_are_not_published() -> None:
    """StartNotify replay of a cached nonzero value cannot create a false key."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char005d"
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {path: bytes.fromhex("f10000")}
    manager._report_metadata_by_path = {path: (1, 0x5D)}
    manager._report_decoder = None
    manager._active_usages_by_path = {}
    manager._active_report_paths = set()
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    manager._async_bluez_value_changed(path, bytes.fromhex("f10000"))
    manager._async_bluez_value_changed(path, bytes.fromhex("000000"))

    assert received == []
    assert manager.last_report is None
    assert manager._initial_cached_values_by_path == {}


def test_first_real_press_differing_from_cached_value_is_published() -> None:
    """Snapshot suppression cannot consume a different first physical press."""
    manager = object.__new__(BluetoothHidRemoteManager)
    manager.address = "00:11:22:33:44:55"
    path = "/org/bluez/hci0/dev_x/service004f/char005d"
    manager._ignored_value_paths = set()
    manager._initial_cached_values_by_path = {path: bytes.fromhex("f10000")}
    manager._report_metadata_by_path = {path: (1, 0x5D)}
    manager._report_decoder = None
    manager._active_usages_by_path = {}
    manager._active_report_paths = set()
    manager.last_report = None
    received: list[HidInputReport] = []
    manager._listeners = {received.append}

    manager._async_bluez_value_changed(path, bytes.fromhex("580000"))

    assert received == [HidInputReport(1, 0x5D, bytes.fromhex("580000"))]
    assert manager._initial_cached_values_by_path == {}


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
