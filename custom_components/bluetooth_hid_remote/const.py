"""Constants for Bluetooth HID Remote."""

from typing import Final

DOMAIN: Final = "bluetooth_hid_remote"

CONF_ADDRESS: Final = "address"
CONF_NAME: Final = "name"

HID_SERVICE_UUID: Final = "00001812-0000-1000-8000-00805f9b34fb"
HID_REPORT_MAP_UUID: Final = "00002a4b-0000-1000-8000-00805f9b34fb"
HID_REPORT_UUID: Final = "00002a4d-0000-1000-8000-00805f9b34fb"
HID_REPORT_REFERENCE_UUID: Final = "00002908-0000-1000-8000-00805f9b34fb"

HID_REPORT_TYPE_INPUT: Final = 1

EVENT_KEY_PRESSED: Final = "key_pressed"
EVENT_KEY_RELEASED: Final = "key_released"
EVENT_TYPES: Final = [EVENT_KEY_PRESSED, EVENT_KEY_RELEASED]

PLATFORMS: Final = ["event"]
