"""Tests for Home Assistant event attributes."""

from custom_components.bluetooth_hid_remote.event import event_attributes
from custom_components.bluetooth_hid_remote.hid import HidUsage
from custom_components.bluetooth_hid_remote.keymap import builtin_key_mapper
from custom_components.bluetooth_hid_remote.manager import HidInputReport


def test_single_decoded_key_adds_convenience_attributes() -> None:
    """A single key remains easy to use in an automation condition."""
    report = HidInputReport(
        1,
        0x5D,
        bytes.fromhex("580000"),
        usages=(HidUsage(0x07, 0x58, "Keypad ENTER"),),
    )

    assert event_attributes(report, builtin_key_mapper("android_tv")) == {
        "report_id": 1,
        "characteristic_handle": 0x5D,
        "data_hex": "580000",
        "key_profile": "android_tv",
        "key_namespace": "android",
        "key_code": 23,
        "key_name": "DPAD_CENTER",
        "hid_usage_page": 0x07,
        "hid_usage_page_hex": "0x0007",
        "hid_usage_page_name": "Keyboard/Keypad",
        "hid_usage_id": 0x58,
        "hid_usage_id_hex": "0x0058",
        "hid_usage_name": "Keypad ENTER",
        "keys": [
            {
                "key_profile": "android_tv",
                "key_namespace": "android",
                "key_code": 23,
                "key_name": "DPAD_CENTER",
                "hid_usage_page": 0x07,
                "hid_usage_page_hex": "0x0007",
                "hid_usage_page_name": "Keyboard/Keypad",
                "hid_usage_id": 0x58,
                "hid_usage_id_hex": "0x0058",
                "hid_usage_name": "Keypad ENTER",
            }
        ],
    }


def test_raw_attributes_are_unchanged_without_decoding() -> None:
    """Unsupported report layouts retain the 0.1.0 event contract."""
    report = HidInputReport(0, 0x5D, bytes.fromhex("580000"))

    assert event_attributes(report, builtin_key_mapper("hid")) == {
        "report_id": 0,
        "characteristic_handle": 0x5D,
        "data_hex": "580000",
    }
