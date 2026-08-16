"""Linux hidraw output reports for a bonded Bluetooth HID remote."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

_HIDRAW_NAME_RE = re.compile(r"hidraw[0-9]+\Z")


class HidRawReportError(Exception):
    """A matching hidraw node could not accept an output report."""


class BluetoothHidRawWriter:
    """Resolve a hidraw node by Bluetooth address and write one report."""

    def __init__(
        self,
        address: str,
        *,
        sys_class_hidraw: Path = Path("/sys/class/hidraw"),
        dev_root: Path = Path("/dev"),
        opener: Callable[[str, int], int] = os.open,
        writer: Callable[[int, bytes], int] = os.write,
        closer: Callable[[int], None] = os.close,
    ) -> None:
        self.address = address.casefold()
        self._sys_class_hidraw = sys_class_hidraw
        self._dev_root = dev_root
        self._opener = opener
        self._writer = writer
        self._closer = closer

    async def async_write(self, report_id: int, payload: bytes) -> str:
        """Write an HID output report without blocking Home Assistant's loop."""
        return await asyncio.to_thread(self._write_sync, report_id, payload)

    async def async_hid_id(self) -> str | None:
        """Return the kernel HID identity for this exact Bluetooth address."""
        return await asyncio.to_thread(self._hid_id_sync)

    def _hid_id_sync(self) -> str | None:
        for _path, properties in self._matching_devices_sync():
            if hid_id := properties.get("HID_ID"):
                return hid_id
        return None

    def _write_sync(self, report_id: int, payload: bytes) -> str:
        report = bytes((report_id,)) + bytes(payload)
        nodes = tuple(path for path, _properties in self._matching_devices_sync())
        if not nodes:
            raise HidRawReportError(
                f"no hidraw node matches Bluetooth address {self.address}"
            )

        errors: list[str] = []
        for path in nodes:
            fd: int | None = None
            try:
                fd = self._opener(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
                written = self._writer(fd, report)
                if written != len(report):
                    raise OSError(f"short hidraw write {written}/{len(report)}")
                return path
            except OSError as err:
                errors.append(f"{path}: {type(err).__name__}: {err}")
            finally:
                if fd is not None:
                    with suppress(OSError):
                        self._closer(fd)

        raise HidRawReportError("; ".join(errors))

    def _matching_devices_sync(self) -> tuple[tuple[str, dict[str, str]], ...]:
        try:
            candidates = tuple(self._sys_class_hidraw.glob("hidraw*"))
        except OSError:
            return ()

        matches: list[tuple[str, dict[str, str]]] = []
        for candidate in candidates:
            if _HIDRAW_NAME_RE.fullmatch(candidate.name) is None:
                continue
            try:
                properties = _parse_uevent(
                    (candidate / "device" / "uevent").read_text()
                )
            except OSError:
                continue
            if properties.get("HID_UNIQ", "").casefold() == self.address:
                matches.append((str(self._dev_root / candidate.name), properties))
        return tuple(sorted(matches, key=lambda item: item[0]))


def _parse_uevent(value: str) -> dict[str, str]:
    """Parse the key/value form exported by Linux sysfs uevent files."""
    properties: dict[str, str] = {}
    for line in value.splitlines():
        key, separator, item = line.partition("=")
        if separator:
            properties[key] = item
    return properties
