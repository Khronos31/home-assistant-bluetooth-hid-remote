"""Observe BlueZ-owned HID-over-GATT connections and input reports."""

from __future__ import annotations

import logging
from asyncio import Lock
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
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from .const import (
    CONF_ADDRESS,
    CONF_NAME,
    EVENT_KEY_PRESSED,
    EVENT_KEY_RELEASED,
    HID_REPORT_UUID,
    HID_SERVICE_UUID,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HidInputReport:
    """One raw input report received from BlueZ."""

    report_id: int
    characteristic_handle: int
    data: bytes

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


class BluetoothHidRemoteManager:
    """Observe a HID connection owned by BlueZ without changing its lifetime."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.name: str = entry.data.get(CONF_NAME, "BLE HID Remote")
        self.connected = False
        self.input_report_count = 0
        self.last_report: HidInputReport | None = None
        self.connection_failures = 0

        self._listeners: set[Callable[[HidInputReport], None]] = set()
        self._unsub_bluetooth: CALLBACK_TYPE | None = None
        self._bluez_manager: BlueZManager | None = None
        self._bluez_watcher: DeviceWatcher | None = None
        self._device_path: str | None = None
        self._report_metadata_by_path: dict[str, tuple[int, int]] = {}
        self._notification_paths: set[str] = set()
        self._notification_lock = Lock()
        self._stopping = False

    async def async_start(self) -> None:
        """Register passive observers for BlueZ and HA Bluetooth discovery."""
        self._stopping = False
        await self._async_register_bluez_watcher()
        self._unsub_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_bluetooth_update,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.PASSIVE,
        )

    async def async_stop(self) -> None:
        """Remove observers without disconnecting the HID remote."""
        self._stopping = True
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

    async def _async_register_bluez_watcher(self) -> None:
        """Observe the direct adapter's BlueZ device object."""
        if self._bluez_watcher is not None:
            return

        device = bluetooth.async_ble_device_from_address(self.hass, self.address, True)
        if device is None:
            _LOGGER.debug(
                "Cannot register BlueZ watcher for %s before first discovery",
                self.address,
            )
            return

        details = device.details
        device_path = details.get("path") if isinstance(details, dict) else None
        if not isinstance(device_path, str):
            _LOGGER.debug("BLE device %s has no BlueZ object path", self.address)
            return

        manager = await get_global_bluez_manager()
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
            self.entry.async_create_background_task(
                self.hass,
                self._async_map_report_characteristics(),
                name=f"Bluetooth HID metadata {self.address}",
            )

    def async_add_listener(
        self, listener: Callable[[HidInputReport], None]
    ) -> CALLBACK_TYPE:
        """Subscribe an entity to input reports."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

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
            self._notification_paths.clear()
            self.input_report_count = 0
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
                requested_services={HID_SERVICE_UUID},
            )
        except Exception:
            self.connection_failures += 1
            _LOGGER.debug(
                "BlueZ did not expose HOGP GATT metadata for %s",
                self.address,
                exc_info=True,
            )
            return

        service = services.get_service(HID_SERVICE_UUID)
        if service is None:
            _LOGGER.debug("BlueZ exposed no HOGP GATT service for %s", self.address)
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

        self._report_metadata_by_path = metadata
        self.input_report_count = len(metadata)
        _LOGGER.debug(
            "Mapped %d BlueZ HOGP report characteristics for %s",
            len(metadata),
            self.address,
        )
        await self._async_start_notifications(notification_paths)

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
                    self.connection_failures += 1
                    _LOGGER.debug(
                        "Could not subscribe to BlueZ HID report %s for %s",
                        path,
                        self.address,
                        exc_info=True,
                    )
                    continue
                if not self.connected or self._stopping:
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

    async def _async_stop_notifications(self) -> None:
        """Release only notification subscriptions created by this manager."""
        manager = self._bluez_manager
        bus = manager._bus if manager is not None else None
        async with self._notification_lock:
            paths = tuple(self._notification_paths)
            self._notification_paths.clear()
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
        self._publish_report(*metadata, value)

    @callback
    def _publish_report(
        self, report_id: int, characteristic_handle: int, data: bytes | bytearray
    ) -> None:
        """Publish one input report to event entities."""
        report = HidInputReport(report_id, characteristic_handle, bytes(data))
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
