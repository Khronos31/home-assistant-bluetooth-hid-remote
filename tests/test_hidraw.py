"""Tests for address-scoped Linux hidraw output reports."""

from __future__ import annotations

import os

import pytest

from custom_components.bluetooth_hid_remote.hidraw import (
    BluetoothHidRawWriter,
    HidRawReportError,
)


@pytest.mark.asyncio
async def test_hidraw_writer_resolves_address_and_prefixes_report_id(tmp_path) -> None:
    """The volatile node number is selected by HID_UNIQ, not hard-coded."""
    sys_class = tmp_path / "sys" / "class" / "hidraw"
    dev_root = tmp_path / "dev"
    wrong = sys_class / "hidraw3" / "device"
    target = sys_class / "hidraw12" / "device"
    wrong.mkdir(parents=True)
    target.mkdir(parents=True)
    dev_root.mkdir()
    (wrong / "uevent").write_text("HID_UNIQ=00:00:00:00:00:00\n")
    (target / "uevent").write_text(
        "HID_ID=0005:00000171:0000042F\nHID_UNIQ=B4:10:7A:66:94:C9\n"
    )
    opened: list[tuple[str, int]] = []
    written: list[tuple[int, bytes]] = []

    writer = BluetoothHidRawWriter(
        "b4:10:7a:66:94:c9",
        sys_class_hidraw=sys_class,
        dev_root=dev_root,
        opener=lambda path, flags: opened.append((path, flags)) or 7,
        writer=lambda fd, value: written.append((fd, value)) or len(value),
        closer=lambda _fd: None,
    )

    path = await writer.async_write(0xF2, b"\x02")
    hid_id = await writer.async_hid_id()

    assert path == str(dev_root / "hidraw12")
    assert opened == [
        (
            str(dev_root / "hidraw12"),
            os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    ]
    assert written == [(7, b"\xf2\x02")]
    assert hid_id == "0005:00000171:0000042F"


@pytest.mark.asyncio
async def test_hidraw_writer_rejects_unmatched_address(tmp_path) -> None:
    """No output can be sent to another Bluetooth device by accident."""
    sys_class = tmp_path / "sys" / "class" / "hidraw"
    candidate = sys_class / "hidraw4" / "device"
    candidate.mkdir(parents=True)
    (candidate / "uevent").write_text("HID_UNIQ=11:22:33:44:55:66\n")
    writer = BluetoothHidRawWriter(
        "b4:10:7a:66:94:c9",
        sys_class_hidraw=sys_class,
        dev_root=tmp_path / "dev",
    )

    with pytest.raises(HidRawReportError, match="no hidraw node matches"):
        await writer.async_write(0xF2, b"\x02")


@pytest.mark.asyncio
async def test_hidraw_writer_reports_open_failure(tmp_path) -> None:
    """Container device-policy failures remain available to the fallback."""
    sys_class = tmp_path / "sys" / "class" / "hidraw"
    candidate = sys_class / "hidraw7" / "device"
    candidate.mkdir(parents=True)
    (candidate / "uevent").write_text("HID_UNIQ=B4:10:7A:66:94:C9\n")
    writer = BluetoothHidRawWriter(
        "b4:10:7a:66:94:c9",
        sys_class_hidraw=sys_class,
        dev_root=tmp_path / "dev",
        opener=lambda _path, _flags: (_ for _ in ()).throw(
            PermissionError(1, "Operation not permitted")
        ),
    )

    with pytest.raises(HidRawReportError, match="Operation not permitted"):
        await writer.async_write(0xF2, b"\x02")
