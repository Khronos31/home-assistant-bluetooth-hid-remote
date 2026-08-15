# Bluetooth HID Remote for Home Assistant

Experimental Home Assistant custom integration for Bluetooth Low Energy
HID-over-GATT (HOGP) remote controls.

The integration uses Home Assistant's directly attached Bluetooth adapter,
observes the HOGP connection owned by BlueZ, and exposes raw button reports
through an event entity without depending on Linux `/dev/input`.

BlueZ's built-in `input-hog` profile owns the remote's wake connection. The
integration never connects, disconnects, or polls the remote; after BlueZ has
connected it, the integration holds notification subscriptions on its input
Report characteristics. A physical BLE remote has delivered press and release
events through this path on Home Assistant OS 2026.8.1.

Current version: **0.1.0**.

## Current development scope

- BLE HOGP service (`0x1812`) only.
- A directly attached HAOS Bluetooth adapter.
- The initial spike requires the remote to be paired and bonded already.
- Event data is raw (`report_id`, characteristic handle, and hex payload).
- BlueZ owns the HOGP connection; the integration must not alter its lifetime.
- Devices with malformed GATT tables may fail BlueZ service discovery and need
  an operating-system-side workaround. This integration does not modify HAOS
  or BlueZ configuration.

Classic Bluetooth HID and first-time pairing through ESPHome Bluetooth Proxy
are not supported.

## Event data

Every press and release keeps the raw 0.1.0 attributes:
`report_id`, `characteristic_handle`, and `data_hex`. When the remote exposes a
readable HID Report Map and Report Reference, the integration also decodes
standard HID usages. A single decoded key adds convenient scalar attributes:

```yaml
event_type: key_pressed
report_id: 1
data_hex: "580000"
usage_page: 7
usage_page_hex: "0x07"
usage_page_name: Keyboard/Keypad
key_code: 88
key_code_hex: "0x58"
key_name: Keypad Enter
```

`key_code` is the numeric **HID Usage ID**, not a Linux evdev code or Android
keycode. Treat `(usage_page, key_code)` as the stable identity; the same Usage
ID can mean something different on another page. `report_id` is the Report
Reference ID when readable and remains `0` when that metadata is unavailable.

`keys` contains the same fields as a list when a report has one or more active
usages. A release report carries the decoded usages from its preceding press,
so automations can match the same `key_code` for both event types. Unknown
usage IDs remain available numerically; unreadable or unsupported descriptors
fall back to the original raw attributes without dropping the event.

## Installation

Before the first tagged release, add this repository to HACS as a custom
**Integration** repository only if you are participating in hardware
validation. Tagged releases are installed through HACS in the usual way.

After installation and a Home Assistant restart, wake a BLE HOGP remote and add
**Bluetooth HID Remote** from Settings > Devices & services. A directly attached
Bluetooth adapter is required; ESPHome Bluetooth proxies cannot transport HID
notifications or perform first-time pairing for this integration.

## Development validation

```bash
python -m pip install -r requirements-test.txt
./scripts/validate.sh
```

`VERSION`, the Home Assistant manifest, and this README are synchronized by the
workflow-dispatched release job. A release remains experimental until more HID
remote models have been tested.

## License

MIT
