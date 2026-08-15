"""Tests for address-scoped Linux evdev ownership."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from custom_components.bluetooth_hid_remote.input_grab import (
    _EVIOCGRAB,
    BluetoothInputGrabber,
)

ADDRESS = "88:34:37:C9:CA:71"


def _sysfs_node(root: Path, event: str, uniq: str) -> None:
    device = root / event / "device"
    device.mkdir(parents=True)
    (device / "uniq").write_text(f"{uniq}\n")


def test_reconcile_grabs_only_all_nodes_with_exact_address(tmp_path: Path) -> None:
    """Every matching interface is grabbed without touching another keyboard."""
    sysfs = tmp_path / "sys"
    dev = tmp_path / "dev"
    dev.mkdir()
    _sysfs_node(sysfs, "event14", ADDRESS.lower())
    _sysfs_node(sysfs, "event15", ADDRESS)
    _sysfs_node(sysfs, "event16", "00:11:22:33:44:55")
    opened: list[tuple[str, int]] = []
    ioctls: list[tuple[int, int, int]] = []
    closed: list[int] = []

    def opener(path: str, flags: int) -> int:
        opened.append((path, flags))
        return 100 + len(opened)

    grabber = BluetoothInputGrabber(
        ADDRESS,
        sys_class_input=sysfs,
        dev_input=dev,
        opener=opener,
        closer=closed.append,
        ioctl=lambda fd, operation, value: ioctls.append((fd, operation, value)),
    )

    status = grabber._reconcile_sync()

    assert status.active is True
    assert status.matching_nodes == (
        str(dev / "event14"),
        str(dev / "event15"),
    )
    assert status.grabbed_nodes == status.matching_nodes
    assert [path for path, _flags in opened] == list(status.matching_nodes)
    assert ioctls == [
        (101, _EVIOCGRAB, 1),
        (102, _EVIOCGRAB, 1),
    ]
    assert closed == []


def test_reconcile_releases_disappeared_node(tmp_path: Path) -> None:
    """A disconnect closes the old descriptor and clears protection state."""
    sysfs = tmp_path / "sys"
    dev = tmp_path / "dev"
    dev.mkdir()
    _sysfs_node(sysfs, "event14", ADDRESS)
    ioctls: list[tuple[int, int, int]] = []
    closed: list[int] = []
    grabber = BluetoothInputGrabber(
        ADDRESS,
        sys_class_input=sysfs,
        dev_input=dev,
        opener=lambda _path, _flags: 101,
        closer=closed.append,
        ioctl=lambda fd, operation, value: ioctls.append((fd, operation, value)),
    )

    assert grabber._reconcile_sync().active is True
    shutil.rmtree(sysfs / "event14")
    status = grabber._reconcile_sync()

    assert status.active is False
    assert status.matching_nodes == ()
    assert status.grabbed_nodes == ()
    assert ioctls[-1] == (101, _EVIOCGRAB, 0)
    assert closed == [101]


def test_failed_grab_is_visible_and_closes_descriptor(tmp_path: Path) -> None:
    """A permission or ownership conflict is diagnostic, not a leaked FD."""
    sysfs = tmp_path / "sys"
    dev = tmp_path / "dev"
    dev.mkdir()
    _sysfs_node(sysfs, "event14", ADDRESS)
    closed: list[int] = []

    def failed_ioctl(_fd: int, _operation: int, _value: int) -> None:
        raise PermissionError("denied")

    grabber = BluetoothInputGrabber(
        ADDRESS,
        sys_class_input=sysfs,
        dev_input=dev,
        opener=lambda _path, _flags: 101,
        closer=closed.append,
        ioctl=failed_ioctl,
    )

    status = grabber._reconcile_sync()

    assert status.active is False
    assert status.grabbed_nodes == ()
    assert status.errors == ((str(dev / "event14"), "PermissionError: denied"),)
    assert closed == [101]


@pytest.mark.asyncio
async def test_stop_releases_every_descriptor(tmp_path: Path) -> None:
    """Config-entry unload explicitly relinquishes kernel ownership."""
    sysfs = tmp_path / "sys"
    dev = tmp_path / "dev"
    dev.mkdir()
    _sysfs_node(sysfs, "event14", ADDRESS)
    ioctls: list[tuple[int, int, int]] = []
    closed: list[int] = []
    grabber = BluetoothInputGrabber(
        ADDRESS,
        sys_class_input=sysfs,
        dev_input=dev,
        opener=lambda _path, _flags: 101,
        closer=closed.append,
        ioctl=lambda fd, operation, value: ioctls.append((fd, operation, value)),
    )
    grabber._publish_status(grabber._reconcile_sync())

    await grabber.async_stop()

    assert grabber.status.active is False
    assert ioctls[-1] == (101, _EVIOCGRAB, 0)
    assert closed == [101]
