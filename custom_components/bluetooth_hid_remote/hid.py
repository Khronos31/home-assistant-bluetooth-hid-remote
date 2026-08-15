"""Minimal HID Report Descriptor decoding for button-oriented input reports."""

from __future__ import annotations

from dataclasses import dataclass

from hid_parser.data import UsagePages


@dataclass(frozen=True, slots=True)
class HidUsage:
    """One active HID usage decoded from an input report."""

    usage_page: int
    usage_id: int
    name: str | None = None

    @property
    def usage_page_name(self) -> str:
        """Return a stable display name for the usage page."""
        return usage_page_name(self.usage_page)


@dataclass(frozen=True, slots=True)
class HidInputField:
    """One Input main item in a HID report descriptor."""

    report_id: int
    bit_offset: int
    report_size: int
    report_count: int
    usage_page: int
    variable: bool
    usages: tuple[tuple[int, int], ...]
    usage_minimum: tuple[int, int] | None
    usage_maximum: tuple[int, int] | None


class HidReportDescriptorError(ValueError):
    """Raised when a HID short or long item is truncated."""


class HidReportDecoder:
    """Decode active usages from input reports described by a Report Map."""

    def __init__(self, fields: tuple[HidInputField, ...]) -> None:
        self._fields_by_report_id: dict[int, list[HidInputField]] = {}
        for field in fields:
            self._fields_by_report_id.setdefault(field.report_id, []).append(field)

    @classmethod
    def from_report_map(cls, report_map: bytes) -> HidReportDecoder:
        """Parse the Input items needed for usage decoding."""
        return cls(_parse_input_fields(report_map))

    def decode(self, report_id: int, data: bytes) -> tuple[HidUsage, ...]:
        """Return active usages from one report payload.

        HOGP Report characteristics identify the Report ID through their Report
        Reference descriptor, so ``data`` does not contain a leading ID byte.
        """
        raw = int.from_bytes(data, "little")
        decoded: list[HidUsage] = []
        for field in self._fields_by_report_id.get(report_id, ()):
            if field.report_size <= 0:
                continue
            mask = (1 << field.report_size) - 1
            for index in range(field.report_count):
                value = (raw >> (field.bit_offset + index * field.report_size)) & mask
                if value == 0:
                    continue
                usage = _usage_for_value(field, index, value)
                if usage is None:
                    continue
                page, usage_id = usage
                decoded.append(HidUsage(page, usage_id, usage_name(page, usage_id)))
        return tuple(decoded)


def _parse_input_fields(report_map: bytes) -> tuple[HidInputField, ...]:
    usage_page = 0
    report_size = 0
    report_count = 0
    report_id = 0
    global_stack: list[tuple[int, int, int, int]] = []
    bit_offsets: dict[int, int] = {}
    local_usages: list[tuple[int, int]] = []
    usage_minimum: tuple[int, int] | None = None
    usage_maximum: tuple[int, int] | None = None
    fields: list[HidInputField] = []

    offset = 0
    while offset < len(report_map):
        prefix = report_map[offset]
        offset += 1
        if prefix == 0xFE:
            if offset + 2 > len(report_map):
                raise HidReportDescriptorError("truncated HID long item header")
            length = report_map[offset]
            offset += 2
            if offset + length > len(report_map):
                raise HidReportDescriptorError("truncated HID long item data")
            offset += length
            continue

        size_code = prefix & 0x03
        size = 4 if size_code == 3 else size_code
        if offset + size > len(report_map):
            raise HidReportDescriptorError("truncated HID short item data")
        item_data = report_map[offset : offset + size]
        offset += size
        value = int.from_bytes(item_data, "little")
        item_type = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F

        if item_type == 1:  # Global
            if tag == 0:
                usage_page = value
            elif tag == 7:
                report_size = value
            elif tag == 8:
                report_id = value
            elif tag == 9:
                report_count = value
            elif tag == 10:
                global_stack.append((usage_page, report_size, report_count, report_id))
            elif tag == 11:
                if not global_stack:
                    raise HidReportDescriptorError("HID global Pop without Push")
                usage_page, report_size, report_count, report_id = global_stack.pop()
            continue

        if item_type == 2:  # Local
            usage = _qualified_usage(usage_page, value, size)
            if tag == 0:
                local_usages.append(usage)
            elif tag == 1:
                usage_minimum = usage
            elif tag == 2:
                usage_maximum = usage
            continue

        if item_type != 0:  # Reserved
            continue

        if tag == 8:  # Input
            bit_offset = bit_offsets.get(report_id, 0)
            constant = bool(value & 0x01)
            if not constant and report_size and report_count:
                fields.append(
                    HidInputField(
                        report_id=report_id,
                        bit_offset=bit_offset,
                        report_size=report_size,
                        report_count=report_count,
                        usage_page=usage_page,
                        variable=bool(value & 0x02),
                        usages=tuple(local_usages),
                        usage_minimum=usage_minimum,
                        usage_maximum=usage_maximum,
                    )
                )
            bit_offsets[report_id] = bit_offset + report_size * report_count

        # Local state is reset after every Main item.
        local_usages = []
        usage_minimum = None
        usage_maximum = None

    return tuple(fields)


def _qualified_usage(page: int, value: int, size: int) -> tuple[int, int]:
    if size == 4 and value > 0xFFFF:
        return value >> 16, value & 0xFFFF
    return page, value


def _usage_for_value(
    field: HidInputField, index: int, value: int
) -> tuple[int, int] | None:
    if not field.variable:
        return field.usage_page, value
    if index < len(field.usages):
        return field.usages[index]
    if field.usage_minimum is not None and field.usage_maximum is not None:
        page, first = field.usage_minimum
        last_page, last = field.usage_maximum
        usage_id = first + index
        if page == last_page and usage_id <= last:
            return page, usage_id
    if field.usages:
        return field.usages[-1]
    return None


def usage_page_name(page: int) -> str:
    """Return the HID Usage Tables page name used in event attributes."""
    try:
        return UsagePages.get_description(page)
    except KeyError:
        return f"Usage Page 0x{page:02X}"


def usage_name(page: int, usage_id: int) -> str | None:
    """Resolve a usage through the comprehensive HID Usage Tables database."""
    try:
        usage_data = UsagePages.get_subdata(page)
        return usage_data.get_description(usage_id)
    except (KeyError, ValueError):
        return None
