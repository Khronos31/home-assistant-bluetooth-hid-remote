"""Key identity profiles built on canonical HID usages."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import load_yaml_dict

from .android_keycodes import ANDROID_KEY_CODES
from .const import (
    KEY_PROFILE_ANDROID_TV,
    KEY_PROFILE_GOOGLE_TV,
    KEY_PROFILE_HID,
    KEYMAP_FILENAME,
)
from .hid import HidUsage

type UsageKey = tuple[int, int]


class KeyMapError(ValueError):
    """Raised for an invalid or unavailable key mapping profile."""


@dataclass(frozen=True, slots=True)
class KeyIdentity:
    """One key expressed in a selected naming and numeric namespace."""

    namespace: str
    code: int
    name: str


@dataclass(frozen=True, slots=True)
class KeyMapper:
    """Resolve canonical HID usages through one selected profile."""

    profile: str
    namespace: str
    base: str
    overrides: Mapping[UsageKey, KeyIdentity]

    def resolve(self, usage: HidUsage) -> KeyIdentity:
        """Resolve one HID usage without hiding its canonical identity."""
        key = (usage.usage_page, usage.usage_id)
        if identity := self.overrides.get(key):
            return identity
        if self.base == KEY_PROFILE_ANDROID_TV:
            return _android_identity(key)
        return _hid_identity(usage)


@dataclass(frozen=True, slots=True)
class CustomKeyProfile:
    """Validated custom profile loaded from YAML."""

    name: str
    namespace: str
    extends: str
    mappings: Mapping[UsageKey, KeyIdentity]

    def mapper(self) -> KeyMapper:
        """Create the runtime mapper for this custom profile."""
        return KeyMapper(self.name, self.namespace, self.extends, self.mappings)


def builtin_key_mapper(profile: str) -> KeyMapper:
    """Create one built-in mapper."""
    if profile == KEY_PROFILE_HID:
        return KeyMapper(profile, "hid", profile, {})
    if profile == KEY_PROFILE_ANDROID_TV:
        return KeyMapper(profile, "android", profile, {})
    if profile == KEY_PROFILE_GOOGLE_TV:
        return KeyMapper(
            profile,
            "android",
            KEY_PROFILE_ANDROID_TV,
            _android_overrides(_GOOGLE_TV_USAGE_NAMES),
        )
    raise KeyMapError(f"unknown built-in key profile: {profile}")


def mapped_key_attributes(mapper: KeyMapper, usage: HidUsage) -> dict[str, Any]:
    """Return namespaced mapped and canonical HID attributes."""
    identity = mapper.resolve(usage)
    return {
        "key_profile": mapper.profile,
        "key_namespace": identity.namespace,
        "key_code": identity.code,
        "key_name": identity.name,
        "hid_usage_page": usage.usage_page,
        "hid_usage_page_hex": f"0x{usage.usage_page:04X}",
        "hid_usage_page_name": usage.usage_page_name,
        "hid_usage_id": usage.usage_id,
        "hid_usage_id_hex": f"0x{usage.usage_id:04X}",
        "hid_usage_name": usage.name,
    }


async def async_load_custom_profiles(
    hass: HomeAssistant,
) -> dict[str, CustomKeyProfile]:
    """Load optional custom profiles from the Home Assistant config directory."""
    path = Path(hass.config.path(KEYMAP_FILENAME))
    if not await hass.async_add_executor_job(path.is_file):
        return {}
    try:
        data = await hass.async_add_executor_job(load_yaml_dict, path)
        return parse_custom_profiles(data)
    except KeyMapError:
        raise
    except Exception as err:
        raise KeyMapError(f"could not load {KEYMAP_FILENAME}: {err}") from err


async def async_create_key_mapper(hass: HomeAssistant, profile: str) -> KeyMapper:
    """Create a built-in or custom mapper by profile name."""
    if profile in (
        KEY_PROFILE_HID,
        KEY_PROFILE_ANDROID_TV,
        KEY_PROFILE_GOOGLE_TV,
    ):
        return builtin_key_mapper(profile)
    profiles = await async_load_custom_profiles(hass)
    try:
        return profiles[profile].mapper()
    except KeyError as err:
        raise KeyMapError(f"custom key profile not found: {profile}") from err


def parse_custom_profiles(data: Mapping[str, Any]) -> dict[str, CustomKeyProfile]:
    """Validate custom key profiles from decoded YAML."""
    if set(data) - {"profiles"}:
        raise KeyMapError("only the top-level 'profiles' key is supported")
    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, Mapping):
        raise KeyMapError("'profiles' must be a mapping")

    profiles: dict[str, CustomKeyProfile] = {}
    for name, raw_profile in raw_profiles.items():
        if (
            not isinstance(name, str)
            or not name
            or name
            in (
                KEY_PROFILE_HID,
                KEY_PROFILE_ANDROID_TV,
                KEY_PROFILE_GOOGLE_TV,
            )
        ):
            raise KeyMapError(f"invalid custom profile name: {name!r}")
        if not isinstance(raw_profile, Mapping):
            raise KeyMapError(f"profile {name!r} must be a mapping")
        if extras := set(raw_profile) - {"extends", "namespace", "mappings"}:
            raise KeyMapError(f"profile {name!r} has unknown keys: {sorted(extras)}")

        extends = raw_profile.get("extends", KEY_PROFILE_HID)
        if extends not in (KEY_PROFILE_HID, KEY_PROFILE_ANDROID_TV):
            raise KeyMapError(
                f"profile {name!r} extends unsupported profile {extends!r}"
            )
        base_namespace = "android" if extends == KEY_PROFILE_ANDROID_TV else "hid"
        namespace = raw_profile.get("namespace", base_namespace)
        if namespace != base_namespace:
            raise KeyMapError(
                f"profile {name!r} namespace must match its {extends!r} base"
            )
        raw_mappings = raw_profile.get("mappings", {})
        if not isinstance(raw_mappings, Mapping):
            raise KeyMapError(f"profile {name!r} mappings must be a mapping")
        mappings: dict[UsageKey, KeyIdentity] = {}
        for raw_key, raw_value in raw_mappings.items():
            usage_key = _parse_usage_key(raw_key)
            mappings[usage_key] = _parse_mapping_value(raw_value, usage_key, namespace)
        profiles[name] = CustomKeyProfile(
            name=name,
            namespace=namespace,
            extends=extends,
            mappings=mappings,
        )
    return profiles


def _parse_usage_key(value: Any) -> UsageKey:
    if not isinstance(value, str):
        raise KeyMapError(f"usage key must be a string, got {value!r}")
    parts = value.split(":")
    if len(parts) != 2:
        raise KeyMapError(f"invalid usage key {value!r}; expected 'PPPP:UUUU'")
    try:
        page, usage_id = (int(part, 16) for part in parts)
    except ValueError as err:
        raise KeyMapError(f"invalid hexadecimal usage key: {value!r}") from err
    if not 0 <= page <= 0xFFFF or not 0 <= usage_id <= 0xFFFF:
        raise KeyMapError(f"usage key out of range: {value!r}")
    return page, usage_id


def _parse_mapping_value(
    value: Any, usage_key: UsageKey, namespace: str
) -> KeyIdentity:
    if isinstance(value, str):
        name = _normalize_android_name(value) if namespace == "android" else value
        if namespace == "android":
            try:
                code = ANDROID_KEY_CODES[name]
            except KeyError as err:
                raise KeyMapError(f"unknown Android KeyEvent name: {value!r}") from err
        else:
            code = usage_key[1]
        return KeyIdentity(namespace, code, name)

    if not isinstance(value, Mapping) or set(value) != {"key_code", "key_name"}:
        raise KeyMapError(
            "mapping values must be a key name or contain key_code and key_name"
        )
    code = value["key_code"]
    name = value["key_name"]
    if isinstance(code, bool) or not isinstance(code, int) or code < 0:
        raise KeyMapError(f"invalid key_code: {code!r}")
    if not isinstance(name, str) or not name.strip():
        raise KeyMapError(f"invalid key_name: {name!r}")
    name = _normalize_android_name(name) if namespace == "android" else name.strip()
    if namespace == "android" and ANDROID_KEY_CODES.get(name) != code:
        raise KeyMapError(f"Android key name/code mismatch: {name} != {code}")
    return KeyIdentity(namespace, code, name)


def _hid_identity(usage: HidUsage) -> KeyIdentity:
    name = _normalize_hid_name(usage.name) if usage.name else _hid_fallback(usage)
    return KeyIdentity("hid", usage.usage_id, name)


def _android_identity(key: UsageKey) -> KeyIdentity:
    name = _ANDROID_TV_USAGE_NAMES.get(key)
    if name is None and key[0] == 0x07:
        compatibility = _LINUX_HID_KEYBOARD_ANDROID_COMPAT.get(key[1])
        if compatibility is not None:
            _, name = compatibility
    if name is None:
        name = "UNKNOWN"
    return KeyIdentity("android", ANDROID_KEY_CODES[name], name)


def _android_overrides(
    usage_names: Mapping[UsageKey, str],
) -> dict[UsageKey, KeyIdentity]:
    """Build a validated Android identity table for one public profile."""
    return {
        usage: KeyIdentity("android", ANDROID_KEY_CODES[name], name)
        for usage, name in usage_names.items()
    }


def _normalize_android_name(name: str) -> str:
    normalized = name.strip().upper()
    return normalized.removeprefix("KEYCODE_")


def _normalize_hid_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _hid_fallback(usage: HidUsage) -> str:
    return f"HID_{usage.usage_page:04X}_{usage.usage_id:04X}"


_ANDROID_TV_USAGE_NAMES: dict[UsageKey, str] = {
    **{(0x07, 0x04 + index): chr(ord("A") + index) for index in range(26)},
    **{(0x07, 0x1E + index): str(index + 1) for index in range(9)},
    (0x07, 0x27): "0",
    (0x07, 0x28): "ENTER",
    (0x07, 0x29): "ESCAPE",
    (0x07, 0x2A): "DEL",
    (0x07, 0x2B): "TAB",
    (0x07, 0x2C): "SPACE",
    **{(0x07, 0x3A + index): f"F{index + 1}" for index in range(12)},
    (0x07, 0x4A): "MOVE_HOME",
    (0x07, 0x4B): "PAGE_UP",
    (0x07, 0x4C): "FORWARD_DEL",
    (0x07, 0x4D): "MOVE_END",
    (0x07, 0x4E): "PAGE_DOWN",
    (0x07, 0x4F): "DPAD_RIGHT",
    (0x07, 0x50): "DPAD_LEFT",
    (0x07, 0x51): "DPAD_DOWN",
    (0x07, 0x52): "DPAD_UP",
    (0x07, 0x54): "NUMPAD_DIVIDE",
    (0x07, 0x55): "NUMPAD_MULTIPLY",
    (0x07, 0x56): "NUMPAD_SUBTRACT",
    (0x07, 0x57): "NUMPAD_ADD",
    (0x07, 0x58): "DPAD_CENTER",
    (0x07, 0x66): "POWER",
    (0x0C, 0x30): "POWER",
    (0x0C, 0x40): "MENU",
    (0x0C, 0x89): "TV",
    (0x0C, 0x8D): "GUIDE",
    (0x0C, 0x9C): "CHANNEL_UP",
    (0x0C, 0x9D): "CHANNEL_DOWN",
    (0x0C, 0xB0): "MEDIA_PLAY",
    (0x0C, 0xB1): "MEDIA_PAUSE",
    (0x0C, 0xB2): "MEDIA_RECORD",
    (0x0C, 0xB3): "MEDIA_FAST_FORWARD",
    (0x0C, 0xB4): "MEDIA_REWIND",
    (0x0C, 0xB5): "MEDIA_NEXT",
    (0x0C, 0xB6): "MEDIA_PREVIOUS",
    (0x0C, 0xB7): "MEDIA_STOP",
    (0x0C, 0xCD): "MEDIA_PLAY_PAUSE",
    (0x0C, 0xE2): "VOLUME_MUTE",
    (0x0C, 0xE9): "VOLUME_UP",
    (0x0C, 0xEA): "VOLUME_DOWN",
    (0x0C, 0x221): "SEARCH",
    (0x0C, 0x223): "HOME",
    (0x0C, 0x224): "BACK",
    (0x0C, 0x225): "FORWARD",
    # Both the genuine Fire TV remote and the tested compatible AR expose
    # their four branded app buttons through this vendor page.  Keep the
    # public names service-neutral while preserving the raw HID identity.
    (0xFF, 0x00A1): "VIDEO_APP_1",
    (0xFF, 0x00A2): "VIDEO_APP_2",
    (0xFF, 0x00A3): "VIDEO_APP_3",
    (0xFF, 0x00A4): "VIDEO_APP_4",
}


# The genuine and tested compatible Google TV remotes use these low usage IDs
# as their button enum instead of their nominal HID Usage Tables meanings. Keep
# this override in a device-family profile so the generic HID and Android TV
# profiles remain standards-based. Every entry below was observed on hardware.
_GOOGLE_TV_USAGE_NAMES: dict[UsageKey, str] = {
    (0x0C, 0x0001): "DPAD_UP",
    (0x0C, 0x0002): "DPAD_DOWN",
    (0x0C, 0x0003): "DPAD_LEFT",
    (0x0C, 0x0004): "DPAD_RIGHT",
    (0x0C, 0x0005): "DPAD_CENTER",
    (0x0C, 0x0006): "BACK",
    (0x0C, 0x0007): "HOME",
    (0x0C, 0x0008): "VOICE_ASSIST",
    (0x07, 0x0001): "VOLUME_UP",
    (0x07, 0x0002): "VOLUME_DOWN",
    (0x0C, 0x0009): "VOLUME_MUTE",
    # Google TV itself delivers these three buttons as BUTTON_3, BUTTON_4 and
    # MACRO_1; the codes were read back on the device with Button Mapper and
    # QuickBars. Match what apps on the TV actually receive.
    (0x0C, 0x000A): "BUTTON_3",
    (0x0C, 0x000B): "BUTTON_4",
    (0x0C, 0x000C): "POWER",
    (0x0C, 0x000D): "MACRO_1",
}


# Linux's legacy HID keyboard compatibility table assigns input-event codes to
# a handful of unassigned Keyboard/Keypad usages. Android then converts those
# Linux codes through AOSP Generic.kl. Keep the intermediate Linux code only as
# implementation provenance; the Android TV profile exposes Android names and
# KeyEvent numbers exclusively. Entries without an active Generic.kl mapping
# (notably usages 0xF4 and 0xF7) intentionally remain UNKNOWN.
_LINUX_HID_KEYBOARD_ANDROID_COMPAT: dict[int, tuple[int, str]] = {
    0xF0: (150, "EXPLORER"),
    0xF1: (158, "BACK"),
    0xF2: (159, "FORWARD"),
    0xF3: (128, "MEDIA_STOP"),
    0xF5: (177, "PAGE_UP"),
    0xF6: (178, "PAGE_DOWN"),
    0xF8: (142, "SLEEP"),
    0xF9: (152, "LOCK"),
    0xFA: (173, "REFRESH"),
    0xFB: (140, "CALCULATOR"),
}


for _android_name in (
    *_ANDROID_TV_USAGE_NAMES.values(),
    *_GOOGLE_TV_USAGE_NAMES.values(),
    *(name for _, name in _LINUX_HID_KEYBOARD_ANDROID_COMPAT.values()),
):
    if _android_name not in ANDROID_KEY_CODES:
        raise RuntimeError(f"unknown built-in Android key name: {_android_name}")
