"""Tests for the locally served integration brand asset."""

import struct
from hashlib import sha256
from pathlib import Path


def test_icon_matches_the_home_assistant_bluetooth_asset() -> None:
    """The custom integration deliberately reuses the standard Bluetooth icon."""
    icon = (
        Path(__file__).parents[1]
        / "custom_components/bluetooth_hid_remote/brand/icon.png"
    )
    data = icon.read_bytes()

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", data[16:24]) == (256, 256)
    assert sha256(data).hexdigest() == (
        "5457aacac2e47480b9544c1ff15327c8a133a30e3924bcad0cd65947e9c5e5b9"
    )
