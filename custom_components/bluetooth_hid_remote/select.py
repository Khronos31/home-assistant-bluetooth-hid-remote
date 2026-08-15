"""Assist pipeline selector for voice-capable Bluetooth HID remotes."""

from __future__ import annotations

from homeassistant.components.assist_pipeline.select import AssistPipelineSelect
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BluetoothHidRemoteConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothHidRemoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a pipeline selector only when the descriptor supports voice."""
    manager = entry.runtime_data
    if manager.supports_voice:
        async_add_entities([BluetoothHidRemotePipelineSelect(hass, manager)])


class BluetoothHidRemotePipelineSelect(AssistPipelineSelect):
    """Select the Assist pipeline used by one remote microphone."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, manager) -> None:
        compact_address = manager.address.replace(":", "").lower()
        super().__init__(hass, DOMAIN, compact_address)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.address)},
            connections={(CONNECTION_BLUETOOTH, manager.address)},
            manufacturer="Bluetooth SIG",
            model="BLE HID Remote",
            name=manager.name,
        )
