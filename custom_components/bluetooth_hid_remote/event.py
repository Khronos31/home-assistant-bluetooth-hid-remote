"""Event entity for Bluetooth HID Remote."""

from __future__ import annotations

from homeassistant.components.event import (
    EventDeviceClass,
    EventEntity,
    EventEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BluetoothHidRemoteConfigEntry
from .const import DOMAIN, EVENT_TYPES
from .manager import HidInputReport

DESCRIPTION = EventEntityDescription(
    key="remote_button",
    translation_key="remote_button",
    device_class=EventDeviceClass.BUTTON,
    event_types=EVENT_TYPES,
)


def event_attributes(report: HidInputReport) -> dict:
    """Build backward-compatible raw and decoded event attributes."""
    attributes = {
        "report_id": report.report_id,
        "characteristic_handle": report.characteristic_handle,
        "data_hex": report.data.hex(),
    }
    keys = [
        {
            "usage_page": usage.usage_page,
            "usage_page_hex": f"0x{usage.usage_page:02X}",
            "usage_page_name": usage.usage_page_name,
            "key_code": usage.usage_id,
            "key_code_hex": f"0x{usage.usage_id:02X}",
            "key_name": usage.name,
        }
        for usage in report.usages
    ]
    if keys:
        attributes["keys"] = keys
    if len(keys) == 1:
        attributes.update(keys[0])
    return attributes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothHidRemoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the remote's event entity."""
    async_add_entities([BluetoothHidRemoteEvent(entry.runtime_data)])


class BluetoothHidRemoteEvent(EventEntity):
    """Expose raw HOGP input reports through a Home Assistant event entity."""

    _attr_has_entity_name = True
    entity_description = DESCRIPTION

    def __init__(self, manager) -> None:
        self._manager = manager
        compact_address = manager.address.replace(":", "").lower()
        self._attr_unique_id = f"{compact_address}_remote_button"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.address)},
            connections={(CONNECTION_BLUETOOTH, manager.address)},
            manufacturer="Bluetooth SIG",
            model="BLE HID Remote",
            name=manager.name,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to input reports."""
        await super().async_added_to_hass()
        self.async_on_remove(self._manager.async_add_listener(self._receive_report))

    @callback
    def _receive_report(self, report: HidInputReport) -> None:
        """Publish one raw report as a press or release event."""
        self._trigger_event(
            report.event_type,
            event_attributes(report),
        )
        self.async_write_ha_state()
