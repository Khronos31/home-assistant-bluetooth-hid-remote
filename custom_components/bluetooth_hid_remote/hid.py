"""Minimal HID Report Descriptor decoding for button-oriented input reports."""

from __future__ import annotations

from dataclasses import dataclass


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
    names = {
        0x01: "Generic Desktop",
        0x06: "Generic Device Controls",
        0x07: "Keyboard/Keypad",
        0x09: "Button",
        0x0C: "Consumer",
    }
    if page >= 0xFF00:
        return "Vendor-defined"
    return names.get(page, f"Usage Page 0x{page:02X}")


def usage_name(page: int, usage_id: int) -> str | None:
    """Resolve common keyboard and consumer usages without hiding unknown IDs."""
    if page == 0x07:
        if 0x04 <= usage_id <= 0x1D:
            return f"Keyboard {chr(ord('A') + usage_id - 0x04)}"
        if 0x1E <= usage_id <= 0x26:
            return f"Keyboard {usage_id - 0x1D}"
        if usage_id == 0x27:
            return "Keyboard 0"
        return _KEYBOARD_NAMES.get(usage_id)
    if page == 0x0C:
        return _CONSUMER_NAMES.get(usage_id)
    return None


_KEYBOARD_NAMES = {
    0x28: "Keyboard Enter",
    0x29: "Keyboard Escape",
    0x2A: "Keyboard Backspace",
    0x2B: "Keyboard Tab",
    0x2C: "Keyboard Space",
    0x3A: "Keyboard F1",
    0x3B: "Keyboard F2",
    0x3C: "Keyboard F3",
    0x3D: "Keyboard F4",
    0x3E: "Keyboard F5",
    0x3F: "Keyboard F6",
    0x40: "Keyboard F7",
    0x41: "Keyboard F8",
    0x42: "Keyboard F9",
    0x43: "Keyboard F10",
    0x44: "Keyboard F11",
    0x45: "Keyboard F12",
    0x4A: "Keyboard Home",
    0x4B: "Keyboard Page Up",
    0x4C: "Keyboard Delete Forward",
    0x4D: "Keyboard End",
    0x4E: "Keyboard Page Down",
    0x4F: "Keyboard Right Arrow",
    0x50: "Keyboard Left Arrow",
    0x51: "Keyboard Down Arrow",
    0x52: "Keyboard Up Arrow",
    0x54: "Keypad Divide",
    0x55: "Keypad Multiply",
    0x56: "Keypad Subtract",
    0x57: "Keypad Add",
    0x58: "Keypad Enter",
}

_CONSUMER_NAMES = {
    0x30: "Power",
    0x40: "Menu",
    0xB0: "Play",
    0xB1: "Pause",
    0xB2: "Record",
    0xB3: "Fast Forward",
    0xB4: "Rewind",
    0xB5: "Scan Next Track",
    0xB6: "Scan Previous Track",
    0xB7: "Stop",
    0xCD: "Play/Pause",
    0xE2: "Mute",
    0xE9: "Volume Increment",
    0xEA: "Volume Decrement",
}
