"""Bluetooth HID Remote integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .manager import BluetoothHidRemoteManager

type BluetoothHidRemoteConfigEntry = ConfigEntry[BluetoothHidRemoteManager]


async def async_setup_entry(
    hass: HomeAssistant, entry: BluetoothHidRemoteConfigEntry
) -> bool:
    """Set up a Bluetooth HID remote from a config entry."""
    manager = BluetoothHidRemoteManager(hass, entry)
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.async_start()
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BluetoothHidRemoteConfigEntry
) -> bool:
    """Unload a Bluetooth HID remote config entry."""
    await entry.runtime_data.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
