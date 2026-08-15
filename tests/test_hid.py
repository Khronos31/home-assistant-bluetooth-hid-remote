"""Tests for HID Report Descriptor usage decoding."""

import pytest

from custom_components.bluetooth_hid_remote.hid import (
    HidReportDecoder,
    HidReportDescriptorError,
    HidUsage,
    usage_name,
    usage_page_name,
)

AR_REPORT_MAP = bytes.fromhex(
    "05010906a1010507850195037508150025ff190029ff8100c0"
    "050c0901a1018502950275101500269c0219002a9c028100c0"
    "06ff000900a10185f095507508150025ff810085f195037508"
    "0900810285f2950175080900910285f3950a75080900910285"
    "ef95037508150025ff190029ff09018100c0050c0901a10185"
    "0305010906a102050609201500266400750895018102c0c0"
)


def test_ar_keyboard_report_decodes_keypad_enter() -> None:
    """AR report ID 1 defines three Keyboard/Keypad array entries."""
    decoder = HidReportDecoder.from_report_map(AR_REPORT_MAP)

    assert decoder.decode(1, bytes.fromhex("580000")) == (
        HidUsage(0x07, 0x58, "Keypad ENTER"),
    )
    assert decoder.decode(1, bytes.fromhex("000000")) == ()


def test_keyboard_array_decodes_multiple_simultaneous_usages() -> None:
    """Each nonzero array slot becomes a separate active usage."""
    decoder = HidReportDecoder.from_report_map(AR_REPORT_MAP)

    assert decoder.decode(1, bytes.fromhex("580400")) == (
        HidUsage(0x07, 0x58, "Keypad ENTER"),
        HidUsage(0x07, 0x04, "Keyboard a and A"),
    )


def test_ar_consumer_report_decodes_volume_increment() -> None:
    """AR report ID 2 defines two 16-bit Consumer array entries."""
    decoder = HidReportDecoder.from_report_map(AR_REPORT_MAP)

    assert decoder.decode(2, bytes.fromhex("e9000000")) == (
        HidUsage(0x0C, 0xE9, "Volume Increment"),
    )


def test_ar_voice_report_declares_eighty_byte_payload() -> None:
    """Voice detection can be anchored in the remote's HID descriptor."""
    decoder = HidReportDecoder.from_report_map(AR_REPORT_MAP)

    assert decoder.input_report_size_bytes(0xF0) == 80
    assert decoder.input_report_size_bytes(0xF1) == 3
    assert decoder.input_report_size_bytes(0x7F) is None


def test_less_common_usage_keeps_numeric_identity_and_gets_a_name() -> None:
    """The comprehensive table names usages outside a hand-curated subset."""
    decoder = HidReportDecoder.from_report_map(AR_REPORT_MAP)

    assert decoder.decode(1, bytes.fromhex("a40000")) == (
        HidUsage(0x07, 0xA4, "Keyboard ExSel"),
    )


def test_variable_button_bits_use_descriptor_usage_positions() -> None:
    """Variable fields map set bits to their corresponding local usages."""
    report_map = bytes.fromhex("05091901290315002501750195038102")
    decoder = HidReportDecoder.from_report_map(report_map)

    assert decoder.decode(0, bytes([0b101])) == (
        HidUsage(0x09, 1, "Button 1 (primary/trigger)"),
        HidUsage(0x09, 3, "Button 3 (tertiary)"),
    )


@pytest.mark.parametrize(
    ("page", "usage_id", "name"),
    [
        (0x07, 0x58, "Keypad ENTER"),
        (0x07, 0x51, "Keyboard DownArrow"),
        (0x07, 0x04, "Keyboard a and A"),
        (0x07, 0x46, "Keyboard PrintScreen"),
        (0x0C, 0x8C, "Media Select Telephone"),
        (0x0C, 0x8D, "Media Select Program Guide"),
        (0x0C, 0xE9, "Volume Increment"),
        (0x0C, 0x223, "AC Home"),
        (0x09, 3, "Button 3 (tertiary)"),
        (0x07, 0xA4, "Keyboard ExSel"),
    ],
)
def test_usage_names_cover_the_hid_usage_tables(
    page: int, usage_id: int, name: str
) -> None:
    """The HID profile uses the comprehensive HID Usage Tables database."""
    assert usage_name(page, usage_id) == name


def test_unknown_usage_names_fall_back_cleanly() -> None:
    """Unknown pages and usages retain their numeric canonical identity."""
    assert usage_name(0xFFFF, 1) is None
    assert usage_name(0x07, 0xFFFF) is None
    assert usage_page_name(0x1234) == "Usage Page 0x1234"


def test_truncated_descriptor_is_rejected() -> None:
    """A malformed item cannot silently create an incorrect field layout."""
    with pytest.raises(HidReportDescriptorError, match="truncated"):
        HidReportDecoder.from_report_map(bytes.fromhex("7508fe04"))
