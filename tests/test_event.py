"""Tests for Home Assistant event attributes."""

from custom_components.bluetooth_hid_remote.event import event_attributes
from custom_components.bluetooth_hid_remote.hid import HidUsage
from custom_components.bluetooth_hid_remote.manager import HidInputReport


def test_single_decoded_key_adds_convenience_attributes() -> None:
    """A single key remains easy to use in an automation condition."""
    report = HidInputReport(
        1,
        0x5D,
        bytes.fromhex("580000"),
        usages=(HidUsage(0x07, 0x58, "Keypad Enter"),),
    )

    assert event_attributes(report) == {
        "report_id": 1,
        "characteristic_handle": 0x5D,
        "data_hex": "580000",
        "usage_page": 0x07,
        "usage_page_hex": "0x07",
        "usage_page_name": "Keyboard/Keypad",
        "key_code": 0x58,
        "key_code_hex": "0x58",
        "key_name": "Keypad Enter",
        "keys": [
            {
                "usage_page": 0x07,
                "usage_page_hex": "0x07",
                "usage_page_name": "Keyboard/Keypad",
                "key_code": 0x58,
                "key_code_hex": "0x58",
                "key_name": "Keypad Enter",
            }
        ],
    }


def test_raw_attributes_are_unchanged_without_decoding() -> None:
    """Unsupported report layouts retain the 0.1.0 event contract."""
    report = HidInputReport(0, 0x5D, bytes.fromhex("580000"))

    assert event_attributes(report) == {
        "report_id": 0,
        "characteristic_handle": 0x5D,
        "data_hex": "580000",
    }
