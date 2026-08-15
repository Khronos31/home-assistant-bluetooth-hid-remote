"""Bluetooth HID Remote integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import (
    CONF_KEY_PROFILE,
    KEY_PROFILE_ANDROID_TV,
    KEY_PROFILE_HID,
    PLATFORMS,
)
from .keymap import KeyMapError, async_create_key_mapper
from .manager import BluetoothHidRemoteManager

type BluetoothHidRemoteConfigEntry = ConfigEntry[BluetoothHidRemoteManager]


async def async_setup_entry(
    hass: HomeAssistant, entry: BluetoothHidRemoteConfigEntry
) -> bool:
    """Set up a Bluetooth HID remote from a config entry."""
    profile = entry.options.get(
        CONF_KEY_PROFILE, entry.data.get(CONF_KEY_PROFILE, KEY_PROFILE_HID)
    )
    try:
        key_mapper = await async_create_key_mapper(hass, profile)
    except KeyMapError as err:
        raise ConfigEntryError(str(err)) from err
    manager = BluetoothHidRemoteManager(hass, entry, key_mapper)
    entry.runtime_data = manager
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    try:
        await manager.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await manager.async_stop()
        raise
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: BluetoothHidRemoteConfigEntry
) -> bool:
    """Unload a Bluetooth HID remote config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_stop()
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: BluetoothHidRemoteConfigEntry
) -> None:
    """Reload an entry after its key profile changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant, entry: BluetoothHidRemoteConfigEntry
) -> bool:
    """Migrate pre-profile entries while preserving remote-oriented behavior."""
    if entry.version == 1:
        data = dict(entry.data)
        data[CONF_KEY_PROFILE] = KEY_PROFILE_ANDROID_TV
        hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True
