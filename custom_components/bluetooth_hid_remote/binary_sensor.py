"""Diagnostic binary sensors for Bluetooth HID Remote."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BluetoothHidRemoteConfigEntry
from .const import DOMAIN
from .input_grab import InputGrabStatus

CONSOLE_INPUT_PROTECTION_DESCRIPTION = BinarySensorEntityDescription(
    key="console_input_protection",
    translation_key="console_input_protection",
    icon="mdi:shield-key",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothHidRemoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up input-protection diagnostics for one remote."""
    async_add_entities([BluetoothHidInputProtectionSensor(entry.runtime_data)])


class BluetoothHidInputProtectionSensor(BinarySensorEntity):
    """Show whether every matching Linux input node is exclusively held."""

    _attr_has_entity_name = True

    def __init__(self, manager) -> None:
        self.entity_description = CONSOLE_INPUT_PROTECTION_DESCRIPTION
        self._manager = manager
        self._status = manager.input_grab_status
        compact_address = manager.address.replace(":", "").lower()
        self._attr_unique_id = f"{compact_address}_console_input_protection"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.address)},
            connections={(CONNECTION_BLUETOOTH, manager.address)},
            manufacturer="Bluetooth SIG",
            model="BLE HID Remote",
            name=manager.name,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to exclusive-input ownership changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._manager.async_add_input_grab_listener(self._receive_status)
        )

    @property
    def is_on(self) -> bool:
        """Return whether all matching event nodes are protected."""
        return self._status.active

    @property
    def extra_state_attributes(self) -> dict:
        """Expose exact matching, acquired, and failed event nodes."""
        return {
            "matching_nodes": list(self._status.matching_nodes),
            "grabbed_nodes": list(self._status.grabbed_nodes),
            "errors": dict(self._status.errors),
        }

    @callback
    def _receive_status(self, status: InputGrabStatus) -> None:
        self._status = status
        self.async_write_ha_state()
