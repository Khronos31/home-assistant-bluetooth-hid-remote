"""Assist pipeline selector for voice-capable Bluetooth HID remotes."""

from __future__ import annotations

from homeassistant.components.assist_pipeline.select import AssistPipelineSelect
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BluetoothHidRemoteConfigEntry
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothHidRemoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a pipeline selector when static metadata proves voice support."""
    manager = entry.runtime_data
    entity_added = False

    @callback
    def async_add_if_supported() -> None:
        nonlocal entity_added
        if entity_added or not manager.supports_voice:
            return
        entity_added = True
        async_add_entities([BluetoothHidRemotePipelineSelect(hass, manager)])

    entry.async_on_unload(
        manager.async_add_voice_support_listener(async_add_if_supported)
    )
    async_add_if_supported()


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
