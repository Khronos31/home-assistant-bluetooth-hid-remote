"""Tests for the explicit AR/HAOS GATT-cache compatibility repair."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bleak.backends.bluezdbus import defs
from dbus_fast.constants import MessageType
from dbus_fast.message import Message
from dbus_fast.signature import Variant

from custom_components.bluetooth_hid_remote.compatibility import (
    CompatibilityRepairUnsupportedError,
    async_install_ar_gatt_cache,
    async_restore_ar_gatt_cache,
    is_ar_cache_repair_candidate,
)
from custom_components.bluetooth_hid_remote.const import HID_SERVICE_UUID

ADDRESS = "88:34:37:C9:CA:71"
ADAPTER_ADDRESS = "AA:BB:CC:DD:EE:FF"
ADAPTER_PATH = "/org/bluez/hci0"
DEVICE_PATH = f"{ADAPTER_PATH}/dev_{ADDRESS.replace(':', '_')}"
UNIT_PATH = "/org/freedesktop/systemd1/unit/bluetooth_2dhid_2dcache_2eservice"


def _manager(
    *, name: str = "AR", manufacturer_data: bytes = bytes.fromhex("041e")
) -> SimpleNamespace:
    return SimpleNamespace(
        _properties={
            ADAPTER_PATH: {defs.ADAPTER_INTERFACE: {"Address": ADAPTER_ADDRESS}},
            DEVICE_PATH: {
                defs.DEVICE_INTERFACE: {
                    "Adapter": ADAPTER_PATH,
                    "Address": ADDRESS,
                    "Appearance": 0x0180,
                    "Bonded": True,
                    "ManufacturerData": {0x0171: manufacturer_data},
                    "Name": name,
                    "Paired": True,
                    "UUIDs": [HID_SERVICE_UUID],
                }
            },
        }
    )


def _reply(*body: object) -> SimpleNamespace:
    return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=list(body))


class FakeBus:
    """Record systemd operations and emulate a successful one-shot helper."""

    def __init__(self) -> None:
        self.calls: list[Message] = []
        self.disconnected = False

    async def call(self, message: Message) -> SimpleNamespace:
        self.calls.append(message)
        if message.member == "GetUnit":
            return _reply(UNIT_PATH)
        if message.member == "Get" and message.body[-1] == "ActiveState":
            return _reply(Variant("s", "active"))
        return _reply()

    def disconnect(self) -> None:
        self.disconnected = True


def test_ar_cache_repair_candidate_is_narrow() -> None:
    """Only the exact tested bonded AR identity enters the host repair path."""
    assert is_ar_cache_repair_candidate(_manager(), DEVICE_PATH) is True
    assert is_ar_cache_repair_candidate(_manager(name="Keyboard"), DEVICE_PATH) is False
    assert (
        is_ar_cache_repair_candidate(
            _manager(manufacturer_data=bytes.fromhex("042f")), DEVICE_PATH
        )
        is False
    )


@pytest.mark.asyncio
async def test_ar_cache_repair_stops_bluez_runs_helper_and_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair waits for its helper and always restores host Bluetooth."""
    bus = FakeBus()
    wait_for_bluez = AsyncMock()
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.compatibility._async_open_system_bus",
        AsyncMock(return_value=bus),
    )
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.compatibility._async_wait_for_bluez",
        wait_for_bluez,
    )
    config_dir = Path(__file__).parents[1]
    hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir)))

    await async_install_ar_gatt_cache(hass, ADDRESS, manager=_manager())

    members = [call.member for call in bus.calls]
    assert members == [
        "StopUnit",
        "StartTransientUnit",
        "GetUnit",
        "Get",
        "StopUnit",
        "StartUnit",
    ]
    helper_call = bus.calls[1]
    exec_start = dict(helper_call.body[2])["ExecStart"].value[0]
    assert exec_start[0] == "/bin/sh"
    assert exec_start[1][-2:] == [ADAPTER_ADDRESS, ADDRESS]
    assert exec_start[1][1].startswith(
        "/mnt/data/supervisor/homeassistant/custom_components/"
    )
    assert exec_start[1][2].endswith("/compatibility/ar-gatt-cache")
    assert wait_for_bluez.await_args_list[0].kwargs == {"running": False}
    assert wait_for_bluez.await_args_list[1].kwargs == {"running": True}
    assert bus.disconnected is True


@pytest.mark.asyncio
async def test_ar_cache_repair_rejects_unrelated_hid() -> None:
    """A generic HOGP device can never trigger the host compatibility mutation."""
    hass = SimpleNamespace(config=SimpleNamespace(config_dir="/config"))

    with pytest.raises(CompatibilityRepairUnsupportedError):
        await async_install_ar_gatt_cache(
            hass, ADDRESS, manager=_manager(name="Keyboard")
        )


@pytest.mark.asyncio
async def test_ar_cache_restore_stops_bluez_runs_helper_and_restarts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit rollback restores only the selected remote's snapshot."""
    bus = FakeBus()
    wait_for_bluez = AsyncMock()
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.compatibility._async_open_system_bus",
        AsyncMock(return_value=bus),
    )
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.compatibility._async_wait_for_bluez",
        wait_for_bluez,
    )
    config_dir = Path(__file__).parents[1]
    hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(config_dir)))

    await async_restore_ar_gatt_cache(hass, ADDRESS, manager=_manager())

    members = [call.member for call in bus.calls]
    assert members == [
        "StopUnit",
        "StartTransientUnit",
        "GetUnit",
        "Get",
        "StopUnit",
        "StartUnit",
    ]
    helper_call = bus.calls[1]
    exec_start = dict(helper_call.body[2])["ExecStart"].value[0]
    assert exec_start[0] == "/bin/sh"
    assert exec_start[1][-3:] == ["restore", ADAPTER_ADDRESS, ADDRESS]
    assert exec_start[1][1].endswith("/compatibility/restore-bluez-cache.sh")
    assert wait_for_bluez.await_args_list[0].kwargs == {"running": False}
    assert wait_for_bluez.await_args_list[1].kwargs == {"running": True}
    assert bus.disconnected is True
