"""Tests for HID, Android TV, and custom key profiles."""

from pathlib import Path

import pytest
from homeassistant.util.yaml import load_yaml_dict

from custom_components.bluetooth_hid_remote.hid import HidUsage
from custom_components.bluetooth_hid_remote.keymap import (
    KeyMapError,
    builtin_key_mapper,
    mapped_key_attributes,
    parse_custom_profiles,
)


@pytest.mark.parametrize(
    ("page", "usage_id", "code", "name"),
    [
        (0x07, 0x58, 23, "DPAD_CENTER"),
        (0x07, 0x51, 20, "DPAD_DOWN"),
        (0x07, 0x66, 26, "POWER"),
        (0x0C, 0x8D, 172, "GUIDE"),
        (0x0C, 0x223, 3, "HOME"),
    ],
)
def test_android_tv_profile_uses_android_keyevent_constants(
    page: int, usage_id: int, code: int, name: str
) -> None:
    """Android TV names and numbers come from one Android namespace."""
    identity = builtin_key_mapper("android_tv").resolve(
        HidUsage(page, usage_id, "Canonical HID name")
    )

    assert (identity.namespace, identity.code, identity.name) == (
        "android",
        code,
        name,
    )


@pytest.mark.parametrize(
    ("usage_id", "code", "name"),
    [
        (0xF0, 64, "EXPLORER"),
        (0xF1, 4, "BACK"),
        (0xF2, 125, "FORWARD"),
        (0xF3, 86, "MEDIA_STOP"),
        (0xF5, 92, "PAGE_UP"),
        (0xF6, 93, "PAGE_DOWN"),
        (0xF8, 223, "SLEEP"),
        (0xF9, 324, "LOCK"),
        (0xFA, 285, "REFRESH"),
        (0xFB, 210, "CALCULATOR"),
    ],
)
def test_android_tv_profile_follows_linux_hid_keyboard_compatibility(
    usage_id: int, code: int, name: str
) -> None:
    """Legacy keyboard usages follow Linux input and Android Generic.kl."""
    identity = builtin_key_mapper("android_tv").resolve(HidUsage(0x07, usage_id, None))

    assert (identity.namespace, identity.code, identity.name) == (
        "android",
        code,
        name,
    )


def test_android_tv_profile_does_not_invent_unmapped_linux_compatibility() -> None:
    """A Linux HID compatibility code absent from Generic.kl stays unknown."""
    identity = builtin_key_mapper("android_tv").resolve(HidUsage(0x07, 0xF4, None))

    assert (identity.namespace, identity.code, identity.name) == (
        "android",
        0,
        "UNKNOWN",
    )


@pytest.mark.parametrize(
    ("page", "usage_id", "code", "name"),
    [
        (0x0C, 0x0001, 19, "DPAD_UP"),
        (0x0C, 0x0002, 20, "DPAD_DOWN"),
        (0x0C, 0x0003, 21, "DPAD_LEFT"),
        (0x0C, 0x0004, 22, "DPAD_RIGHT"),
        (0x0C, 0x0005, 23, "DPAD_CENTER"),
        (0x0C, 0x0006, 4, "BACK"),
        (0x0C, 0x0007, 3, "HOME"),
        (0x0C, 0x0008, 231, "VOICE_ASSIST"),
        (0x07, 0x0001, 24, "VOLUME_UP"),
        (0x07, 0x0002, 25, "VOLUME_DOWN"),
        (0x0C, 0x0009, 164, "VOLUME_MUTE"),
        (0x0C, 0x000A, 289, "VIDEO_APP_1"),
        (0x0C, 0x000B, 290, "VIDEO_APP_2"),
        (0x0C, 0x000C, 26, "POWER"),
        (0x0C, 0x000D, 174, "BOOKMARK"),
    ],
)
def test_google_tv_profile_uses_observed_genuine_remote_button_enum(
    page: int, usage_id: int, code: int, name: str
) -> None:
    """The public Google profile maps all fifteen observed physical buttons."""
    identity = builtin_key_mapper("google_tv").resolve(
        HidUsage(page, usage_id, "Nominal HID name")
    )

    assert (identity.namespace, identity.code, identity.name) == (
        "android",
        code,
        name,
    )


def test_google_tv_profile_falls_back_to_android_tv_mapping() -> None:
    """Standard usages outside Google's enum retain Android TV behavior."""
    identity = builtin_key_mapper("google_tv").resolve(
        HidUsage(0x0C, 0x00CD, "Play/Pause")
    )

    assert (identity.namespace, identity.code, identity.name) == (
        "android",
        85,
        "MEDIA_PLAY_PAUSE",
    )


def test_hid_profile_keeps_legacy_keyboard_usage_in_the_hid_namespace() -> None:
    """The compatibility bridge never leaks Android semantics into HID mode."""
    identity = builtin_key_mapper("hid").resolve(HidUsage(0x07, 0xF1, None))

    assert (identity.namespace, identity.code, identity.name) == (
        "hid",
        0xF1,
        "HID_0007_00F1",
    )


def test_hid_profile_normalizes_the_canonical_usage_name() -> None:
    """The HID profile never substitutes a Linux or Android numeric code."""
    identity = builtin_key_mapper("hid").resolve(HidUsage(0x07, 0x58, "Keypad ENTER"))

    assert (identity.namespace, identity.code, identity.name) == (
        "hid",
        0x58,
        "KEYPAD_ENTER",
    )


def test_android_unknown_keeps_canonical_hid_attributes() -> None:
    """An unmapped Android key is explicit while HID identity remains visible."""
    attributes = mapped_key_attributes(
        builtin_key_mapper("android_tv"),
        HidUsage(0x07, 0xA4, "Keyboard ExSel"),
    )

    assert attributes["key_code"] == 0
    assert attributes["key_name"] == "UNKNOWN"
    assert attributes["hid_usage_page"] == 0x07
    assert attributes["hid_usage_id"] == 0xA4
    assert attributes["hid_usage_name"] == "Keyboard ExSel"


def test_custom_android_profile_accepts_aosp_names() -> None:
    """A custom YAML profile can override usages with AOSP KeyEvent names."""
    profile = parse_custom_profiles(
        {
            "profiles": {
                "living_room": {
                    "extends": "android_tv",
                    "namespace": "android",
                    "mappings": {
                        "07:0058": "KEYCODE_ENTER",
                        "00FF:00A1": "VIDEO_APP_1",
                        "00FF:00A2": "VIDEO_APP_2",
                        "00FF:00A3": "VIDEO_APP_3",
                        "00FF:00A4": "VIDEO_APP_4",
                        "0C:008D": {"key_code": 172, "key_name": "GUIDE"},
                    },
                }
            }
        }
    )["living_room"].mapper()

    assert profile.resolve(HidUsage(0x07, 0x58, "Keypad ENTER")).name == "ENTER"
    assert profile.resolve(HidUsage(0x07, 0x58, "Keypad ENTER")).code == 66
    assert [
        profile.resolve(HidUsage(0xFF, usage_id, None)).name
        for usage_id in range(0xA1, 0xA5)
    ] == ["VIDEO_APP_1", "VIDEO_APP_2", "VIDEO_APP_3", "VIDEO_APP_4"]
    assert profile.resolve(HidUsage(0x0C, 0x8D, "Guide")).name == "GUIDE"
    assert profile.resolve(HidUsage(0x07, 0x51, "Down")).name == "DPAD_DOWN"


def test_documented_example_is_a_valid_profile_file() -> None:
    """The shipped custom YAML example remains accepted by the parser."""
    example = Path(__file__).parents[1] / "examples/bluetooth_hid_remote_keymaps.yaml"

    profiles = parse_custom_profiles(load_yaml_dict(example))

    assert set(profiles) == {"my_android_remote", "my_hid_remote"}


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"wrong": {}}, "top-level"),
        ({"profiles": {"hid": {}}}, "invalid custom profile"),
        ({"profiles": {"google_tv": {}}}, "invalid custom profile"),
        (
            {"profiles": {"mine": {"mappings": {"not-a-usage": "ENTER"}}}},
            "usage key",
        ),
        (
            {
                "profiles": {
                    "mine": {
                        "extends": "android_tv",
                        "mappings": {"07:0058": {"key_code": 23, "key_name": "ENTER"}},
                    }
                }
            },
            "mismatch",
        ),
    ],
)
def test_invalid_custom_profiles_are_rejected(data: dict, message: str) -> None:
    """Invalid user dictionaries fail before a config entry is reloaded."""
    with pytest.raises(KeyMapError, match=message):
        parse_custom_profiles(data)
