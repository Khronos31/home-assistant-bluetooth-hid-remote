# Bluetooth HID Remote for Home Assistant

Development-stage Home Assistant custom integration for Bluetooth Low Energy
HID-over-GATT (HOGP) remote controls.

The integration uses Home Assistant's directly attached Bluetooth adapter,
subscribes to HID Report characteristics, and exposes button input through an
event entity. It does not depend on Linux creating a `/dev/input` device.

Current version: **0.0.0**.

## Current development scope

- BLE HOGP service (`0x1812`) only.
- A directly attached HAOS Bluetooth adapter.
- The initial spike requires the remote to be paired and bonded already.
- Event data is raw (`report_id`, characteristic handle, and hex payload).

Classic Bluetooth HID and first-time pairing through ESPHome Bluetooth Proxy
are not supported.

## Installation

No stable release exists yet. During development, add this repository to HACS
as a custom **Integration** repository only if you are participating in hardware
validation. Stable releases will be installed through HACS in the usual way.

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
workflow-dispatched release job. A stable release is intentionally blocked
until a physical remote has delivered a button event to Home Assistant.

## License

MIT
