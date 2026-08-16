"""Constants for Bluetooth HID Remote."""

from typing import Final

DOMAIN: Final = "bluetooth_hid_remote"

CONF_ADDRESS: Final = "address"
CONF_KEY_PROFILE: Final = "key_profile"
CONF_NAME: Final = "name"
CONF_VOICE_RESPONSE_PLAYER: Final = "voice_response_player"

KEY_PROFILE_ANDROID_TV: Final = "android_tv"
KEY_PROFILE_GOOGLE_TV: Final = "google_tv"
KEY_PROFILE_HID: Final = "hid"
KEYMAP_FILENAME: Final = "bluetooth_hid_remote_keymaps.yaml"

HID_SERVICE_UUID: Final = "00001812-0000-1000-8000-00805f9b34fb"
HID_REPORT_MAP_UUID: Final = "00002a4b-0000-1000-8000-00805f9b34fb"
HID_REPORT_UUID: Final = "00002a4d-0000-1000-8000-00805f9b34fb"
HID_REPORT_REFERENCE_UUID: Final = "00002908-0000-1000-8000-00805f9b34fb"

HID_REPORT_TYPE_INPUT: Final = 1
HID_REPORT_TYPE_OUTPUT: Final = 2

# Android TV Voice (ATVV) GATT service used by genuine Google TV remotes.
# Audio remains in a bounded in-memory Assist session and is never entity state.
ATVV_SERVICE_UUID: Final = "ab5e0001-5a21-4f05-bc7d-af01f617b664"
ATVV_TX_UUID: Final = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
ATVV_RX_UUID: Final = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
ATVV_CONTROL_UUID: Final = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

EVENT_KEY_PRESSED: Final = "key_pressed"
EVENT_KEY_RELEASED: Final = "key_released"
EVENT_TYPES: Final = [EVENT_KEY_PRESSED, EVENT_KEY_RELEASED]

PLATFORMS: Final = [
    "assist_satellite",
    "binary_sensor",
    "event",
    "select",
    "sensor",
]
