"""Tests for raw HOGP report helpers."""

from custom_components.bluetooth_hid_remote.const import (
    EVENT_KEY_PRESSED,
    EVENT_KEY_RELEASED,
)
from custom_components.bluetooth_hid_remote.manager import (
    HidInputReport,
    event_type_for_report,
    parse_report_reference,
)


def test_report_reference() -> None:
    """A Report Reference contains an ID and report type."""
    assert parse_report_reference(bytes([7, 1])) == (7, 1)
    assert parse_report_reference(b"") is None
    assert parse_report_reference(bytes([1, 2, 3])) is None


def test_report_event_type() -> None:
    """Nonzero input is a press and all-zero input is a release."""
    assert event_type_for_report(bytes.fromhex("510000")) == EVENT_KEY_PRESSED
    assert event_type_for_report(bytes.fromhex("000000")) == EVENT_KEY_RELEASED


def test_input_report_properties() -> None:
    """The immutable report object exposes the spike classification."""
    report = HidInputReport(1, 94, bytes.fromhex("510000"))
    assert report.report_id == 1
    assert report.characteristic_handle == 94
    assert report.event_type == EVENT_KEY_PRESSED
