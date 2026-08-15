"""Observe BlueZ-owned HID-over-GATT connections and input reports."""

from __future__ import annotations

import logging
from asyncio import Lock, TimerHandle
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bleak.backends.bluezdbus import defs
from bleak.backends.bluezdbus.manager import (
    BlueZManager,
    DeviceWatcher,
    get_global_bluez_manager,
)
from bleak.backends.bluezdbus.utils import assert_gatt_reply, assert_reply
from dbus_fast.message import Message
from dbus_fast.signature import Variant
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import (
    ATVV_CONTROL_UUID,
    ATVV_RX_UUID,
    ATVV_SERVICE_UUID,
    ATVV_TX_UUID,
    CONF_ADDRESS,
    CONF_NAME,
    EVENT_KEY_PRESSED,
    EVENT_KEY_RELEASED,
    HID_REPORT_MAP_UUID,
    HID_REPORT_REFERENCE_UUID,
    HID_REPORT_TYPE_INPUT,
    HID_REPORT_UUID,
    HID_SERVICE_UUID,
)
from .hid import HidReportDecoder, HidUsage
from .input_grab import BluetoothInputGrabber, InputGrabStatus
from .keymap import KeyMapper
from .voice import (
    ATVV_CODEC_ADPCM_16K,
    VOICE_PACKET_SIZE,
    VOICE_REPORT_ID,
    AtvvImaAdpcmDecoder,
    HidVoicePacket,
    PcmVoicePacket,
    VoicePacket,
    VoiceTransportError,
    is_supported_opus_packet,
)

_LOGGER = logging.getLogger(__name__)

type VoiceListener = Callable[[VoicePacket | None], None]

_ATVV_CHARACTERISTIC_KINDS = {
    ATVV_TX_UUID: "tx",
    ATVV_RX_UUID: "audio",
    ATVV_CONTROL_UUID: "control",
}
_ATVV_GET_CAPS = bytes.fromhex("0a0100000300")
_ATVV_MIC_OPEN_CAPTURE = bytes.fromhex("0c01")
_ATVV_MIC_CLOSE_ON_REQUEST = bytes.fromhex("0d00")
_ATVV_CAPTURE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class HidInputReport:
    """One raw input report received from BlueZ."""

    report_id: int
    characteristic_handle: int
    data: bytes
    usages: tuple[HidUsage, ...] = ()

    @property
    def event_type(self) -> str:
        """Classify an all-zero report as release, otherwise press."""
        return event_type_for_report(self.data)


def event_type_for_report(data: bytes) -> str:
    """Classify a raw HID report for the event entity."""
    return EVENT_KEY_PRESSED if any(data) else EVENT_KEY_RELEASED


def characteristic_handle_from_path(path: str) -> int | None:
    """Extract a BlueZ GATT characteristic handle from its object path."""
    marker = "/char"
    if marker not in path:
        return None
    suffix = path.rsplit(marker, 1)[1]
    try:
        return int(suffix, 16)
    except ValueError:
        return None


def parse_report_reference(value: bytes) -> tuple[int, int] | None:
    """Parse a two-byte HOGP Report Reference descriptor."""
    if len(value) != 2:
        return None
    return value[0], value[1]


def bluez_device_path_from_address(manager: BlueZManager, address: str) -> str | None:
    """Find a directly attached BlueZ device even if HA prefers a proxy."""
    normalized_address = address.casefold()
    for path, interfaces in manager._properties.items():
        device = interfaces.get(defs.DEVICE_INTERFACE)
        if device and device.get("Address", "").casefold() == normalized_address:
            return path
    return None


class BluetoothHidRemoteManager:
    """Observe a HID connection owned by BlueZ without changing its lifetime."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, key_mapper: KeyMapper
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.key_mapper = key_mapper
        self.address: str = entry.data[CONF_ADDRESS]
        self.name: str = entry.data.get(CONF_NAME, "BLE HID Remote")
        self.connected = False
        self.input_report_count = 0
        self.report_map: bytes | None = None
        self.last_report: HidInputReport | None = None
        self.last_voice_packet: VoicePacket | None = None
        self.connection_failures = 0
        self.input_grabber = BluetoothInputGrabber(
            self.address,
            task_factory=lambda coroutine, name: entry.async_create_background_task(
                hass, coroutine, name=name
            ),
        )

        self._listeners: set[Callable[[HidInputReport], None]] = set()
        self._voice_listeners: set[VoiceListener] = set()
        self._unsub_bluetooth: CALLBACK_TYPE | None = None
        self._bluez_manager: BlueZManager | None = None
        self._bluez_watcher: DeviceWatcher | None = None
        self._device_path: str | None = None
        self._report_metadata_by_path: dict[str, tuple[int, int]] = {}
        self._atvv_paths: dict[str, str] = {}
        self._atvv_tx_path: str | None = None
        self._atvv_close_timer: TimerHandle | None = None
        self._atvv_audio_packet_count = 0
        self._atvv_capture_active = False
        self._atvv_decoder: AtvvImaAdpcmDecoder | None = None
        self._atvv_frame_bytes: int | None = None
        self._ignored_value_paths: set[str] = set()
        self._initial_cached_values_by_path: dict[str, bytes] = {}
        self._report_decoder: HidReportDecoder | None = None
        self._active_usages_by_path: dict[str, tuple[HidUsage, ...]] = {}
        self._active_report_paths: set[str] = set()
        self._notification_paths: set[str] = set()
        self._notification_lock = Lock()
        self._stopping = False

    async def async_start(self) -> None:
        """Register passive observers for BlueZ and HA Bluetooth discovery."""
        self._stopping = False
        await self.input_grabber.async_start()
        try:
            await self._async_register_bluez_watcher()
            self._unsub_bluetooth = bluetooth.async_register_callback(
                self.hass,
                self._async_bluetooth_update,
                BluetoothCallbackMatcher(address=self.address),
                BluetoothScanningMode.PASSIVE,
            )
        except Exception:
            await self.input_grabber.async_stop()
            raise

    async def async_stop(self) -> None:
        """Remove observers without disconnecting the HID remote."""
        if self._atvv_close_timer is not None:
            self._cancel_atvv_capture()
            try:
                await self._async_write_atvv(
                    _ATVV_MIC_CLOSE_ON_REQUEST, "MIC_CLOSE_ON_UNLOAD"
                )
            except Exception:
                _LOGGER.debug(
                    "Could not close ATVV capture while unloading %s",
                    self.address,
                    exc_info=True,
                )
        self._stopping = True
        try:
            if self._unsub_bluetooth is not None:
                self._unsub_bluetooth()
                self._unsub_bluetooth = None
            await self._async_stop_notifications()
            if self._bluez_manager is not None and self._bluez_watcher is not None:
                self._bluez_manager.remove_device_watcher(self._bluez_watcher)
            self._bluez_watcher = None
            self._bluez_manager = None
            self._device_path = None
            self.connected = False
            self._publish_voice_packet(None)
            self._active_usages_by_path.clear()
            self._active_report_paths.clear()
            self._atvv_paths.clear()
            self._ignored_value_paths.clear()
            self._initial_cached_values_by_path.clear()
        finally:
            await self.input_grabber.async_stop()

    @property
    def input_grab_status(self) -> InputGrabStatus:
        """Return the current console-input protection state."""
        return self.input_grabber.status

    def async_add_input_grab_listener(
        self, listener: Callable[[InputGrabStatus], None]
    ) -> CALLBACK_TYPE:
        """Subscribe an entity to console-input protection changes."""
        return self.input_grabber.async_add_listener(listener)

    async def _async_register_bluez_watcher(self) -> None:
        """Observe the direct adapter's BlueZ device object."""
        if self._bluez_watcher is not None:
            return

        manager = await get_global_bluez_manager()
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, True)
        details = device.details if device is not None else None
        device_path = details.get("path") if isinstance(details, dict) else None
        if not isinstance(device_path, str):
            device_path = bluez_device_path_from_address(manager, self.address)
        if device_path is None:
            _LOGGER.debug(
                "Direct BlueZ device %s is unavailable; waiting for discovery",
                self.address,
            )
            return

        self._bluez_watcher = manager.add_device_watcher(
            device_path,
            self._async_bluez_connected_changed,
            self._async_bluez_value_changed,
        )
        self._bluez_manager = manager
        self._device_path = device_path
        self.connected = manager.is_connected(device_path)
        _LOGGER.debug(
            "Registered passive BlueZ HID watcher for %s at %s connected=%s",
            self.address,
            device_path,
            self.connected,
        )
        if self.connected:
            # Initial platform setup needs descriptor metadata so voice-only
            # reports never briefly surface as ordinary key events.
            await self._async_map_report_characteristics()

    def async_add_listener(
        self, listener: Callable[[HidInputReport], None]
    ) -> CALLBACK_TYPE:
        """Subscribe an entity to input reports."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def async_add_voice_listener(self, listener: VoiceListener) -> CALLBACK_TYPE:
        """Subscribe to validated voice packets and stream termination."""
        self._voice_listeners.add(listener)
        return lambda: self._voice_listeners.discard(listener)

    @property
    def supports_voice(self) -> bool:
        """Return whether HID Opus or Android TV Voice is available."""
        supports_hid_opus = bool(
            self._report_decoder is not None
            and self._report_decoder.input_report_size_bytes(VOICE_REPORT_ID)
            == VOICE_PACKET_SIZE
        )
        supports_atvv = bool(
            getattr(self, "_atvv_tx_path", None) is not None
            and {"audio", "control"}.issubset(
                set(getattr(self, "_atvv_paths", {}).values())
            )
        )
        return supports_hid_opus or supports_atvv

    @callback
    def _async_bluetooth_update(self, *_: Any) -> None:
        """Retry passive watcher registration after first discovery."""
        if self._bluez_watcher is not None:
            return
        self.entry.async_create_background_task(
            self.hass,
            self._async_register_bluez_watcher(),
            name=f"Bluetooth HID watcher {self.address}",
        )

    @callback
    def _async_bluez_connected_changed(self, connected: bool) -> None:
        """Track the connection BlueZ's HID profile owns."""
        self.connected = connected
        _LOGGER.debug("BlueZ HID connection %s connected=%s", self.address, connected)
        if not connected:
            self._report_metadata_by_path.clear()
            self._atvv_paths.clear()
            self._cancel_atvv_capture()
            self._ignored_value_paths.clear()
            self._initial_cached_values_by_path.clear()
            self._active_usages_by_path.clear()
            self._active_report_paths.clear()
            self._notification_paths.clear()
            self.input_report_count = 0
            self._publish_voice_packet(None)
            return
        if self._stopping:
            return
        self.entry.async_create_background_task(
            self.hass,
            self._async_map_report_characteristics(),
            name=f"Bluetooth HID metadata {self.address}",
        )

    async def _async_map_report_characteristics(self) -> None:
        """Map HOGP report paths without opening or closing a BLE connection."""
        manager = self._bluez_manager
        device_path = self._device_path
        if manager is None or device_path is None:
            return
        try:
            services = await manager.get_services(
                device_path,
                use_cached=False,
                requested_services={HID_SERVICE_UUID, ATVV_SERVICE_UUID},
            )
        except Exception:
            self.connection_failures += 1
            _LOGGER.debug(
                "BlueZ did not expose HOGP GATT metadata for %s",
                self.address,
                exc_info=True,
            )
            return

        atvv_notification_paths = self._map_atvv_characteristics(
            services.get_service(ATVV_SERVICE_UUID)
        )
        service = services.get_service(HID_SERVICE_UUID)
        if service is None:
            _LOGGER.debug("BlueZ exposed no HOGP GATT service for %s", self.address)
            await self._async_start_notifications(atvv_notification_paths)
            return

        metadata: dict[str, tuple[int, int]] = {}
        notification_paths: list[str] = []
        for characteristic in sorted(
            service.characteristics, key=lambda item: item.handle
        ):
            if characteristic.uuid.lower() != HID_REPORT_UUID:
                continue
            if not ({"notify", "indicate"} & set(characteristic.properties)):
                continue
            obj = characteristic.obj
            if not isinstance(obj, tuple) or not obj or not isinstance(obj[0], str):
                continue
            metadata[obj[0]] = (0, characteristic.handle)
            notification_paths.append(obj[0])

        await self._async_load_hid_metadata(service, metadata)

        self._report_metadata_by_path = metadata
        self.input_report_count = len(metadata)
        _LOGGER.debug(
            "Mapped %d BlueZ HOGP report characteristics for %s",
            len(metadata),
            self.address,
        )
        await self._async_start_notifications(
            [*notification_paths, *atvv_notification_paths]
        )
        if self._atvv_tx_path is not None:
            # StartNotify may echo an old cached value. It has completed now,
            # so the identical response to this fresh request must be parsed.
            for path in atvv_notification_paths:
                self._initial_cached_values_by_path.pop(path, None)
            await self._async_write_atvv(_ATVV_GET_CAPS, "GET_CAPS")

    def _map_atvv_characteristics(self, service) -> list[str]:
        """Map Google's proprietary voice paths without sending commands."""
        paths: dict[str, str] = {}
        notifications: list[str] = []
        self._atvv_tx_path = None
        if service is None:
            self._atvv_paths = paths
            _LOGGER.debug("BlueZ exposed no ATVV GATT service for %s", self.address)
            return notifications

        discovered: list[str] = []
        for characteristic in sorted(
            service.characteristics, key=lambda item: item.handle
        ):
            uuid = characteristic.uuid.lower()
            kind = _ATVV_CHARACTERISTIC_KINDS.get(uuid)
            if kind is None:
                continue
            discovered.append(f"{kind}@{characteristic.handle}")
            if kind == "tx":
                self._atvv_tx_path = self._gatt_object_path(characteristic)
                continue
            if not ({"notify", "indicate"} & set(characteristic.properties)):
                continue
            path = self._gatt_object_path(characteristic)
            if path is None:
                continue
            paths[path] = kind
            notifications.append(path)

        self._atvv_paths = paths
        _LOGGER.debug(
            "Detected ATVV GATT service for %s characteristics=%s",
            self.address,
            ",".join(discovered) or "none",
        )
        return notifications

    async def _async_load_hid_metadata(self, service, metadata) -> None:
        """Read static Report Map and Report References on BlueZ's connection."""
        if self._report_decoder is None:
            report_map = service.get_characteristic(HID_REPORT_MAP_UUID)
            path = self._gatt_object_path(report_map)
            if path is not None:
                # BlueZ reports the value read below through the same watcher
                # used for notifications. It is descriptor metadata, not a key.
                self._ignored_value_paths.add(path)
                try:
                    self.report_map = await self._async_read_bluez_value(
                        path, defs.GATT_CHARACTERISTIC_INTERFACE
                    )
                    self._report_decoder = HidReportDecoder.from_report_map(
                        self.report_map
                    )
                    _LOGGER.debug(
                        "Parsed %d-byte HID Report Map for %s",
                        len(self.report_map),
                        self.address,
                    )
                except Exception:
                    _LOGGER.debug(
                        "Could not load HID Report Map for %s",
                        self.address,
                        exc_info=True,
                    )

        for characteristic in service.characteristics:
            path = self._gatt_object_path(characteristic)
            if path not in metadata:
                continue
            descriptor = next(
                (
                    item
                    for item in characteristic.descriptors
                    if item.uuid.lower() == HID_REPORT_REFERENCE_UUID
                ),
                None,
            )
            descriptor_path = self._gatt_object_path(descriptor)
            if descriptor_path is None:
                continue
            try:
                reference = parse_report_reference(
                    await self._async_read_bluez_value(
                        descriptor_path, defs.GATT_DESCRIPTOR_INTERFACE
                    )
                )
            except Exception:
                _LOGGER.debug(
                    "Could not read HID Report Reference %s for %s",
                    descriptor_path,
                    self.address,
                    exc_info=True,
                )
                continue
            if reference is None or reference[1] != HID_REPORT_TYPE_INPUT:
                continue
            metadata[path] = (reference[0], characteristic.handle)

    @staticmethod
    def _gatt_object_path(gatt_object) -> str | None:
        if gatt_object is None:
            return None
        obj = gatt_object.obj
        if not isinstance(obj, tuple) or not obj or not isinstance(obj[0], str):
            return None
        return obj[0]

    async def _async_read_bluez_value(self, path: str, interface: str) -> bytes:
        """Read one GATT value through the existing global BlueZ D-Bus client."""
        manager = self._bluez_manager
        bus = manager._bus if manager is not None else None
        if bus is None or not bus.connected or not self.connected or self._stopping:
            raise RuntimeError("BlueZ HID connection is not available")
        reply = await bus.call(
            Message(
                destination=defs.BLUEZ_SERVICE,
                path=path,
                interface=interface,
                member="ReadValue",
                signature="a{sv}",
                body=[{}],
            )
        )
        assert_gatt_reply(reply)
        return bytes(reply.body[0])

    async def _async_write_atvv(self, value: bytes, operation: str) -> None:
        """Write one bounded ATVV control command over BlueZ's connection."""
        manager = self._bluez_manager
        bus = manager._bus if manager is not None else None
        path = self._atvv_tx_path
        if (
            bus is None
            or not bus.connected
            or not self.connected
            or self._stopping
            or path is None
        ):
            _LOGGER.debug(
                "Skipped ATVV %s for disconnected %s", operation, self.address
            )
            return
        reply = await bus.call(
            Message(
                destination=defs.BLUEZ_SERVICE,
                path=path,
                interface=defs.GATT_CHARACTERISTIC_INTERFACE,
                member="WriteValue",
                signature="aya{sv}",
                body=[bytes(value), {"type": Variant("s", "command")}],
            )
        )
        assert_gatt_reply(reply)
        _LOGGER.debug(
            "Sent ATVV %s address=%s bytes=%d", operation, self.address, len(value)
        )

    async def _async_start_atvv_capture(self) -> None:
        """Open one short bounded capture after a genuine remote search."""
        if self._atvv_capture_active:
            _LOGGER.debug("Ignored overlapping ATVV search for %s", self.address)
            return
        self._cancel_atvv_capture()
        self._atvv_audio_packet_count = 0
        self._atvv_decoder = None
        try:
            await self._async_write_atvv(_ATVV_MIC_OPEN_CAPTURE, "MIC_OPEN")
        except Exception:
            _LOGGER.debug(
                "Could not send ATVV MIC_OPEN to %s", self.address, exc_info=True
            )
            return
        self._atvv_capture_active = True
        self._atvv_close_timer = self.hass.loop.call_later(
            _ATVV_CAPTURE_SECONDS,
            self._schedule_atvv_close,
        )

    @callback
    def _schedule_atvv_close(self) -> None:
        """Schedule the bounded capture close from the timer callback."""
        self._atvv_close_timer = None
        self.entry.async_create_background_task(
            self.hass,
            self._async_close_atvv_capture(),
            name=f"Close ATVV capture {self.address}",
        )

    async def _async_close_atvv_capture(self) -> None:
        """Close the on-request stream and report only its packet count."""
        try:
            await self._async_write_atvv(_ATVV_MIC_CLOSE_ON_REQUEST, "MIC_CLOSE")
        except Exception:
            _LOGGER.debug(
                "Could not send ATVV MIC_CLOSE to %s", self.address, exc_info=True
            )
        finally:
            self._atvv_capture_active = False
            _LOGGER.debug(
                "ATVV capture address=%s audio_packets=%d",
                self.address,
                self._atvv_audio_packet_count,
            )

    @callback
    def _cancel_atvv_capture(self) -> None:
        """Cancel only the local close timer during disconnect or unload."""
        if self._atvv_close_timer is not None:
            self._atvv_close_timer.cancel()
            self._atvv_close_timer = None
        self._atvv_capture_active = False
        self._atvv_decoder = None

    async def _async_start_notifications(self, paths: list[str]) -> None:
        """Subscribe on BlueZ's existing connection without owning its lifetime."""
        manager = self._bluez_manager
        bus = manager._bus if manager is not None else None
        if bus is None or not bus.connected or not self.connected or self._stopping:
            return

        async with self._notification_lock:
            for path in paths:
                if not self.connected or self._stopping:
                    return
                if path in self._notification_paths:
                    continue
                self._capture_initial_cached_value(path)
                try:
                    reply = await bus.call(
                        Message(
                            destination=defs.BLUEZ_SERVICE,
                            path=path,
                            interface=defs.GATT_CHARACTERISTIC_INTERFACE,
                            member="StartNotify",
                        )
                    )
                    assert_gatt_reply(reply, start_notify=True)
                except Exception:
                    self._initial_cached_values_by_path.pop(path, None)
                    self.connection_failures += 1
                    _LOGGER.debug(
                        "Could not subscribe to BlueZ HID report %s for %s",
                        path,
                        self.address,
                        exc_info=True,
                    )
                    continue
                if not self.connected or self._stopping:
                    self._initial_cached_values_by_path.pop(path, None)
                    try:
                        reply = await bus.call(
                            Message(
                                destination=defs.BLUEZ_SERVICE,
                                path=path,
                                interface=defs.GATT_CHARACTERISTIC_INTERFACE,
                                member="StopNotify",
                            )
                        )
                        assert_reply(reply)
                    except Exception:
                        _LOGGER.debug(
                            "Could not release late BlueZ HID subscription %s for %s",
                            path,
                            self.address,
                            exc_info=True,
                        )
                    return
                self._notification_paths.add(path)
                _LOGGER.debug(
                    "Subscribed to BlueZ HID report %s for %s", path, self.address
                )

    def _capture_initial_cached_value(self, path: str) -> None:
        """Snapshot BlueZ's pre-subscription value for one input report."""
        manager = self._bluez_manager
        interfaces = manager._properties.get(path, {}) if manager is not None else {}
        properties = interfaces.get(defs.GATT_CHARACTERISTIC_INTERFACE, {})
        if "Value" not in properties:
            self._initial_cached_values_by_path.pop(path, None)
            return
        try:
            self._initial_cached_values_by_path[path] = bytes(properties["Value"])
        except (TypeError, ValueError):
            self._initial_cached_values_by_path.pop(path, None)

    async def _async_stop_notifications(self) -> None:
        """Release only notification subscriptions created by this manager."""
        manager = self._bluez_manager
        bus = manager._bus if manager is not None else None
        async with self._notification_lock:
            paths = tuple(self._notification_paths)
            self._notification_paths.clear()
            self._initial_cached_values_by_path.clear()
            if bus is None or not bus.connected or not self.connected:
                return
            for path in paths:
                try:
                    reply = await bus.call(
                        Message(
                            destination=defs.BLUEZ_SERVICE,
                            path=path,
                            interface=defs.GATT_CHARACTERISTIC_INTERFACE,
                            member="StopNotify",
                        )
                    )
                    assert_reply(reply)
                except Exception:
                    _LOGGER.debug(
                        "Could not stop BlueZ HID report subscription %s for %s",
                        path,
                        self.address,
                        exc_info=True,
                    )

    @callback
    def _async_bluez_value_changed(self, path: str, value: bytes) -> None:
        """Publish a report already subscribed by BlueZ's HID profile."""
        if path in self._ignored_value_paths:
            _LOGGER.debug("Ignored static HID metadata value path=%s", path)
            return
        atvv_kind = getattr(self, "_atvv_paths", {}).get(path)
        if path in self._initial_cached_values_by_path:
            initial_value = self._initial_cached_values_by_path.pop(path)
            if value == initial_value:
                if atvv_kind is not None:
                    _LOGGER.debug(
                        "Ignored initial cached ATVV value path=%s kind=%s bytes=%d",
                        path,
                        atvv_kind,
                        len(value),
                    )
                else:
                    _LOGGER.debug(
                        "Ignored initial cached HID value path=%s data=%s",
                        path,
                        value.hex(),
                    )
                return
        if atvv_kind is not None:
            if atvv_kind == "control":
                raw_opcode = value[0] if value else None
                opcode = f"0x{raw_opcode:02x}" if raw_opcode is not None else "empty"
                _LOGGER.debug(
                    "ATVV control address=%s opcode=%s bytes=%d",
                    self.address,
                    opcode,
                    len(value),
                )
                if raw_opcode == 0x0B and len(value) >= 9:
                    self._atvv_frame_bytes = int.from_bytes(value[5:7], "big")
                    _LOGGER.debug(
                        "ATVV capabilities address=%s version=%d.%d codecs=0x%02x "
                        "interaction=0x%02x frame_bytes=%d extra=0x%02x",
                        self.address,
                        value[1],
                        value[2],
                        value[3],
                        value[4],
                        self._atvv_frame_bytes,
                        value[7],
                    )
                elif raw_opcode == 0x08:
                    self.entry.async_create_background_task(
                        self.hass,
                        self._async_start_atvv_capture(),
                        name=f"Open ATVV capture {self.address}",
                    )
                elif raw_opcode == 0x04 and len(value) >= 4:
                    codec = value[2]
                    self._atvv_decoder = (
                        AtvvImaAdpcmDecoder() if codec == ATVV_CODEC_ADPCM_16K else None
                    )
                    _LOGGER.debug(
                        "ATVV audio start address=%s reason=0x%02x codec=0x%02x "
                        "stream_id=%d",
                        self.address,
                        value[1],
                        value[2],
                        value[3],
                    )
                    if self._atvv_decoder is None:
                        _LOGGER.warning(
                            "Ignored unsupported ATVV audio codec 0x%02x from %s",
                            codec,
                            self.address,
                        )
                elif raw_opcode == 0x0A and len(value) >= 7:
                    codec = value[1]
                    if codec == ATVV_CODEC_ADPCM_16K:
                        predictor = int.from_bytes(value[4:6], "big", signed=True)
                        step_index = value[6]
                        try:
                            decoder = self._atvv_decoder or AtvvImaAdpcmDecoder()
                            decoder.reset(predictor, step_index)
                            self._atvv_decoder = decoder
                        except VoiceTransportError:
                            self._atvv_decoder = None
                            _LOGGER.warning(
                                "Ignored invalid ATVV synchronization from %s",
                                self.address,
                            )
                    _LOGGER.debug(
                        "ATVV audio sync address=%s codec=0x%02x sequence=%d",
                        self.address,
                        codec,
                        int.from_bytes(value[2:4], "big"),
                    )
                elif raw_opcode == 0x00:
                    _LOGGER.debug(
                        "ATVV audio stop address=%s reason=%s",
                        self.address,
                        f"0x{value[1]:02x}" if len(value) >= 2 else "unspecified",
                    )
                    self._cancel_atvv_capture()
                    self._publish_voice_packet(None)
            else:
                self._atvv_audio_packet_count += 1
                decoder = getattr(self, "_atvv_decoder", None)
                if decoder is None:
                    return
                frame_bytes = getattr(self, "_atvv_frame_bytes", None)
                if frame_bytes and len(value) > frame_bytes:
                    _LOGGER.warning(
                        "Ignored oversized ATVV audio packet from %s: %d > %d",
                        self.address,
                        len(value),
                        frame_bytes,
                    )
                    return
                try:
                    pcm = decoder.decode(value)
                except VoiceTransportError as err:
                    self._atvv_decoder = None
                    _LOGGER.warning(
                        "Stopped ATVV audio decode for %s: %s", self.address, err
                    )
                    self._publish_voice_packet(None)
                    return
                self._publish_voice_packet(PcmVoicePacket(16_000, pcm))
                if self._atvv_audio_packet_count == 1:
                    _LOGGER.debug(
                        "ATVV audio address=%s encoded_bytes=%d pcm_bytes=%d",
                        self.address,
                        len(value),
                        len(pcm),
                    )
            return
        metadata = self._report_metadata_by_path.get(path)
        if metadata is None:
            handle = characteristic_handle_from_path(path)
            if handle is None:
                _LOGGER.debug(
                    "Ignored BlueZ value without a characteristic handle: %s", path
                )
                return
            metadata = (0, handle)
            _LOGGER.debug(
                "Received unmapped BlueZ HID value path=%s data=%s",
                path,
                value.hex(),
            )
        report_id, characteristic_handle = metadata
        if (
            report_id == VOICE_REPORT_ID
            and self.supports_voice
            and is_supported_opus_packet(value)
        ):
            self._publish_voice_packet(
                HidVoicePacket(report_id, characteristic_handle, bytes(value))
            )
            return
        if any(value):
            self._active_report_paths.add(path)
        elif path not in self._active_report_paths:
            _LOGGER.debug("Ignored idle HID release value path=%s", path)
            return
        else:
            self._active_report_paths.discard(path)
        usages: tuple[HidUsage, ...] = ()
        if self._report_decoder is not None:
            usages = self._report_decoder.decode(report_id, value)
        if usages:
            self._active_usages_by_path[path] = usages
        elif any(value):
            self._active_usages_by_path.pop(path, None)
        else:
            usages = self._active_usages_by_path.pop(path, ())
        self._publish_report(report_id, characteristic_handle, value, usages)

    @callback
    def _publish_report(
        self,
        report_id: int,
        characteristic_handle: int,
        data: bytes | bytearray,
        usages: tuple[HidUsage, ...] = (),
    ) -> None:
        """Publish one input report to event entities."""
        report = HidInputReport(
            report_id, characteristic_handle, bytes(data), usages=usages
        )
        self.last_report = report
        _LOGGER.debug(
            "HID input address=%s report_id=%d handle=%d data=%s",
            self.address,
            report_id,
            characteristic_handle,
            report.data.hex(),
        )
        for listener in tuple(self._listeners):
            listener(report)

    @callback
    def _publish_voice_packet(self, packet: VoicePacket | None) -> None:
        """Publish voice outside the key-event and last-key pipelines."""
        self.last_voice_packet = packet
        if isinstance(packet, HidVoicePacket):
            _LOGGER.debug(
                "HID voice address=%s report_id=%d handle=%d bytes=%d",
                self.address,
                packet.report_id,
                packet.characteristic_handle,
                len(packet.data),
            )
        for listener in tuple(self._voice_listeners):
            listener(packet)
