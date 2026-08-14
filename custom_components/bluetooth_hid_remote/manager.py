"""BLE connection and raw HID report notifications."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BLEAK_RETRY_EXCEPTIONS,
    BleakClientWithServiceCache,
    establish_connection,
)
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import (
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

_LOGGER = logging.getLogger(__name__)

_RECONNECT_MAX_SECONDS: Final = 60


@dataclass(frozen=True, slots=True)
class HidInputReport:
    """One raw input report received over HOGP."""

    report_id: int
    characteristic_handle: int
    data: bytes

    @property
    def event_type(self) -> str:
        """Classify an all-zero report as release, otherwise press."""
        return event_type_for_report(self.data)


def event_type_for_report(data: bytes) -> str:
    """Classify a raw HID report for the spike event entity."""
    return EVENT_KEY_PRESSED if any(data) else EVENT_KEY_RELEASED


def parse_report_reference(value: bytes) -> tuple[int, int] | None:
    """Parse the two-octet HID Report Reference descriptor."""
    if len(value) != 2:
        return None
    return value[0], value[1]


class BluetoothHidRemoteManager:
    """Own one BLE HID connection and its input subscriptions."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.name: str = entry.data.get(CONF_NAME, "BLE HID Remote")
        self.connected = False
        self.report_map: bytes | None = None
        self.input_report_count = 0
        self.last_report: HidInputReport | None = None
        self.connection_attempts = 0
        self.connection_failures = 0

        self._client: BleakClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._listeners: set[Callable[[HidInputReport], None]] = set()
        self._unsub_bluetooth: CALLBACK_TYPE | None = None
        self._advertisement = asyncio.Event()

    async def async_start(self) -> None:
        """Watch advertisements and start a bounded connection loop."""
        self._unsub_bluetooth = bluetooth.async_register_callback(
            self.hass,
            self._async_bluetooth_update,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.PASSIVE,
        )
        self._task = self.entry.async_create_background_task(
            self.hass,
            self._connection_loop(),
            name=f"Bluetooth HID Remote {self.address}",
        )

    async def async_stop(self) -> None:
        """Stop reconnecting and release the BLE link."""
        if self._unsub_bluetooth is not None:
            self._unsub_bluetooth()
            self._unsub_bluetooth = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._async_disconnect()

    def async_add_listener(
        self, listener: Callable[[HidInputReport], None]
    ) -> CALLBACK_TYPE:
        """Subscribe an entity to input reports."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _async_bluetooth_update(self, *_: Any) -> None:
        """Wake the connection loop when the sleeping remote advertises."""
        self._advertisement.set()

    async def _connection_loop(self) -> None:
        """Maintain one connection without a tight retry loop."""
        delay = 1
        while True:
            try:
                await self._async_connect()
                delay = 1
                while self._client is not None and self._client.is_connected:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except BLEAK_RETRY_EXCEPTIONS as err:
                self.connection_failures += 1
                _LOGGER.debug(
                    "BLE HID Remote %s connection failed: %s", self.address, err
                )
            except Exception:
                self.connection_failures += 1
                _LOGGER.debug(
                    "Unexpected BLE HID Remote connection failure", exc_info=True
                )
            finally:
                await self._async_disconnect()

            self._advertisement.clear()
            try:
                async with asyncio.timeout(delay):
                    await self._advertisement.wait()
            except TimeoutError:
                pass
            delay = min(delay * 2, _RECONNECT_MAX_SECONDS)

    async def _async_connect(self) -> None:
        """Connect through HA's selected local scanner and subscribe reports."""
        device: BLEDevice | None = bluetooth.async_ble_device_from_address(
            self.hass, self.address, True
        )
        if device is None:
            raise HomeAssistantError("Remote is not visible to a connectable scanner")

        self.connection_attempts += 1
        self._client = await establish_connection(
            BleakClientWithServiceCache,
            device,
            self.name,
            disconnected_callback=self._disconnected,
            max_attempts=1,
            use_services_cache=False,
        )

        service = self._client.services.get_service(HID_SERVICE_UUID)
        if service is None:
            raise HomeAssistantError("Connected device has no HOGP service")

        report_map = service.get_characteristic(HID_REPORT_MAP_UUID)
        if report_map is not None:
            self.report_map = bytes(await self._client.read_gatt_char(report_map))
            _LOGGER.debug(
                "Read %d-byte HID Report Map from %s",
                len(self.report_map),
                self.address,
            )

        subscribed = 0
        for characteristic in service.characteristics:
            if characteristic.uuid.lower() != HID_REPORT_UUID:
                continue
            reference = await self._async_report_reference(characteristic)
            if reference is None or reference[1] != HID_REPORT_TYPE_INPUT:
                continue
            report_id = reference[0]
            await self._client.start_notify(
                characteristic,
                lambda sender, data, report_id=report_id: self._report_notify(
                    report_id, sender, data
                ),
            )
            subscribed += 1

        if not subscribed:
            raise HomeAssistantError("HOGP service has no subscribable input reports")
        self.input_report_count = subscribed
        self.connected = True
        _LOGGER.info(
            "Bluetooth HID Remote %s connected with %d input reports",
            self.address,
            subscribed,
        )

    async def _async_report_reference(
        self, characteristic: BleakGATTCharacteristic
    ) -> tuple[int, int] | None:
        """Read a Report Reference descriptor for one Report characteristic."""
        descriptor = next(
            (
                item
                for item in characteristic.descriptors
                if item.uuid.lower() == HID_REPORT_REFERENCE_UUID
            ),
            None,
        )
        if descriptor is None:
            return None
        assert self._client is not None
        return parse_report_reference(
            bytes(await self._client.read_gatt_descriptor(descriptor.handle))
        )

    @callback
    def _report_notify(
        self,
        report_id: int,
        sender: BleakGATTCharacteristic,
        data: bytearray,
    ) -> None:
        """Publish one raw input notification to event entities."""
        report = HidInputReport(report_id, sender.handle, bytes(data))
        self.last_report = report
        _LOGGER.debug(
            "HID input address=%s report_id=%d handle=%d data=%s",
            self.address,
            report_id,
            sender.handle,
            report.data.hex(),
        )
        for listener in tuple(self._listeners):
            listener(report)

    def _disconnected(self, _: BleakClient) -> None:
        """Schedule connection-state cleanup from Bleak's callback."""
        self.hass.loop.call_soon_threadsafe(self._mark_disconnected)

    @callback
    def _mark_disconnected(self) -> None:
        self.connected = False

    async def _async_disconnect(self) -> None:
        client, self._client = self._client, None
        self._mark_disconnected()
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception:
                _LOGGER.debug("BLE HID Remote disconnect failed", exc_info=True)
