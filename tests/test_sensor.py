"""Tests for Bluetooth HID Remote last-key sensors."""

from types import SimpleNamespace

from custom_components.bluetooth_hid_remote.hid import HidUsage
from custom_components.bluetooth_hid_remote.keymap import builtin_key_mapper
from custom_components.bluetooth_hid_remote.manager import HidInputReport
from custom_components.bluetooth_hid_remote.sensor import (
    BluetoothHidRemoteLastKeyCodeSensor,
    BluetoothHidRemoteLastKeySensor,
)


def _manager():
    return SimpleNamespace(
        address="88:34:37:C9:CA:71",
        name="AR",
        key_mapper=builtin_key_mapper("android_tv"),
    )


def _report(data: str, usages: tuple[HidUsage, ...]) -> HidInputReport:
    return HidInputReport(1, 0x5D, bytes.fromhex(data), usages)


def test_key_sensors_retain_last_decoded_press() -> None:
    """The sensors expose symbol and numeric Usage ID without clearing on release."""
    usage = HidUsage(0x07, 0x58, "Keypad ENTER")
    press = _report("580000", (usage,))
    release = _report("000000", (usage,))
    symbol_sensor = BluetoothHidRemoteLastKeySensor(_manager())
    code_sensor = BluetoothHidRemoteLastKeyCodeSensor(_manager())
    symbol_writes: list[None] = []
    code_writes: list[None] = []
    symbol_sensor.async_write_ha_state = lambda: symbol_writes.append(None)
    code_sensor.async_write_ha_state = lambda: code_writes.append(None)

    symbol_sensor._receive_report(press)
    code_sensor._receive_report(press)
    symbol_sensor._receive_report(release)
    code_sensor._receive_report(release)
    symbol_sensor._receive_report(press)
    code_sensor._receive_report(press)

    assert symbol_sensor.native_value == "DPAD_CENTER"
    assert code_sensor.native_value == 23
    assert symbol_sensor.force_update is True
    assert code_sensor.force_update is True
    assert len(symbol_writes) == 2
    assert len(code_writes) == 2
    assert symbol_sensor.extra_state_attributes == {
        "report_id": 1,
        "characteristic_handle": 0x5D,
        "data_hex": "580000",
        "key_profile": "android_tv",
        "key_namespace": "android",
        "key_code": 23,
        "key_name": "DPAD_CENTER",
        "hid_usage_page": 0x07,
        "hid_usage_page_hex": "0x0007",
        "hid_usage_page_name": "Keyboard/Keypad",
        "hid_usage_id": 0x58,
        "hid_usage_id_hex": "0x0058",
        "hid_usage_name": "Keypad ENTER",
    }


def test_key_sensors_ignore_raw_only_reports() -> None:
    """An undecodable report cannot overwrite a valid decoded sensor state."""
    usage = HidUsage(0x07, 0x51, "Keyboard DownArrow")
    sensor = BluetoothHidRemoteLastKeySensor(_manager())
    sensor.async_write_ha_state = lambda: None

    sensor._receive_report(_report("510000", (usage,)))
    sensor._receive_report(_report("010000", ()))

    assert sensor.native_value == "DPAD_DOWN"
