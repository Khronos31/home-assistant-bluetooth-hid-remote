"""Tests for Bluetooth HID console-input protection diagnostics."""

from types import SimpleNamespace

from homeassistant.helpers.entity import EntityCategory

from custom_components.bluetooth_hid_remote.binary_sensor import (
    BluetoothHidInputProtectionSensor,
)
from custom_components.bluetooth_hid_remote.input_grab import InputGrabStatus


def test_input_protection_sensor_exposes_exact_grab_state() -> None:
    """The diagnostic distinguishes matching nodes from acquired nodes."""
    status = InputGrabStatus(
        matching_nodes=("/dev/input/event14", "/dev/input/event15"),
        grabbed_nodes=("/dev/input/event14",),
        errors=(("/dev/input/event15", "PermissionError: denied"),),
    )
    manager = SimpleNamespace(
        address="88:34:37:C9:CA:71",
        name="AR",
        input_grab_status=status,
    )
    sensor = BluetoothHidInputProtectionSensor(manager)
    writes: list[None] = []
    sensor.async_write_ha_state = lambda: writes.append(None)

    assert sensor.is_on is False
    assert sensor.entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.extra_state_attributes == {
        "matching_nodes": ["/dev/input/event14", "/dev/input/event15"],
        "grabbed_nodes": ["/dev/input/event14"],
        "errors": {"/dev/input/event15": "PermissionError: denied"},
    }

    protected = InputGrabStatus(
        matching_nodes=("/dev/input/event14", "/dev/input/event15"),
        grabbed_nodes=("/dev/input/event14", "/dev/input/event15"),
    )
    sensor._receive_status(protected)

    assert sensor.is_on is True
    assert writes == [None]
