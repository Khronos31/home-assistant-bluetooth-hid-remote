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

Current version: **0.2.0**.

## Current development scope

- BLE HOGP service (`0x1812`) only.
- A directly attached HAOS Bluetooth adapter.
- First-time pairing from the Home Assistant config flow through BlueZ.
- Event data always retains the raw report and canonical HID identity.
- Selectable HID, Android TV / Fire TV, and Google TV key profiles, plus
  optional custom YAML maps.
- BlueZ owns the HOGP connection; the integration must not alter its lifetime.
- Devices with malformed GATT tables may fail BlueZ service discovery. One
  tested `AR` remote has an explicit, opt-in HAOS compatibility repair; other
  models are never given that host-side workaround.

Classic Bluetooth HID and pairing through ESPHome Bluetooth Proxy are not
supported. The pairing flow currently supports confirmation/Just Works style
HOGP remotes; devices that require typing a PIN or passkey are rejected.

The supported product class is **BLE television/media remote controls**. This
is not intended to become a general-purpose Bluetooth input integration:

- A keyboard using confirmation/Just Works pairing may happen to work, but
  keyboard support is best-effort. Pairing flows that require entering or
  comparing a PIN/passkey are not implemented.
- Do not add a mouse or other pointer device. BlueZ and the generic HOGP path
  may allow one to connect, but pointer reports, buttons, motion, and resulting
  HAOS behavior are unsupported.

Push-to-talk Assist is supported on four separately tested physical devices:
genuine and compatible Fire TV remotes, and genuine and compatible Google TV
remotes. A compatible remote is treated as an independent protocol
implementation, not assumed equivalent from its appearance.

## Key profiles and event data

Every press and release keeps the raw 0.1.0 attributes:
`report_id`, `characteristic_handle`, and `data_hex`. When the remote exposes a
readable HID Report Map and Report Reference, the integration also decodes
standard HID usages through the comprehensive HID Usage Tables database.

The selected profile controls only the convenient `key_code` and `key_name`
fields. Canonical `hid_usage_*` fields are always present, so profile changes
never discard the identity reported by the hardware.

The built-in profiles are:

- **HID**: numeric HID Usage ID and a normalized HID Usage name. This never
  claims that a number is a Linux evdev or Android code.
- **Android TV / Fire TV**: Android `KeyEvent` names and numeric constants for
  common TV remote keys. It also follows Android's real Linux HID compatibility
  path for the legacy Keyboard/Keypad usages used by some remotes; for example,
  HID `0x0007:0x00F1` becomes Android `BACK`/`4`. The four vendor-page app
  buttons confirmed on both the genuine Fire TV remote and compatible AR map
  to service-neutral `VIDEO_APP_1` through `VIDEO_APP_4`. Intermediate Linux
  input codes are never exposed. Unmapped usages are explicit as `UNKNOWN`/`0`
  while the HID fields remain available. This is the default for new entries.
- **Google TV**: extends the Android key namespace with the proprietary
  low-numbered HID usages observed identically on the genuine and tested
  compatible Google TV remotes. Its fifteen verified buttons include
  navigation, volume, voice assist, power, and the three programmable buttons
  Google TV itself reports as `BUTTON_3`, `BUTTON_4`, and `MACRO_1`.
  Unobserved usages retain the Android TV fallback.

Optional custom YAML profiles remain supported for unusual hardware and local
overrides. They are an advanced compatibility escape hatch, not required for
the project's tested hardware once an observed mapping can be attributed to a
supported public device family.

Existing 0.1.x entries migrate to Android TV / Fire TV to preserve their
previous remote-oriented names, and new entries use the same default. Change
the profile from the integration's Configure dialog. With Android TV / Fire TV
selected, a single decoded key looks like:

```yaml
event_type: key_pressed
report_id: 1
data_hex: "580000"
key_profile: android_tv
key_namespace: android
key_code: 23
key_name: DPAD_CENTER
hid_usage_page: 7
hid_usage_page_hex: "0x0007"
hid_usage_page_name: Keyboard/Keypad
hid_usage_id: 88
hid_usage_id_hex: "0x0058"
hid_usage_name: Keypad ENTER
```

Treat `(hid_usage_page, hid_usage_id)` as the stable hardware identity; the
same Usage ID can mean something different on another page. `report_id` is the
Report Reference ID when readable and remains `0` when that metadata is
unavailable.

`keys` contains the same fields as a list when a report has one or more active
usages. A release report carries the decoded usages from its preceding press,
so automations can match the same `key_code` for both event types. Unknown
usage IDs remain available numerically; unreadable or unsupported descriptors
fall back to the original raw attributes without dropping the event.

When notification subscriptions start, BlueZ may replay the value already
cached on a Report characteristic. The integration snapshots that value before
subscribing and suppresses only an identical first replay. A different first
value is treated immediately as a physical press, so startup protection does
not add a general debounce or delay.

Each remote also creates **Last key** and **Last key code** sensor entities.
They retain the latest decoded press instead of clearing on release and use
the selected profile's matching name and numeric namespace. The raw report and
canonical HID identity remain available as sensor attributes. Repeated presses
force an update even when the value stays the same.

### Custom profiles

Copy
[`examples/bluetooth_hid_remote_keymaps.yaml`](examples/bluetooth_hid_remote_keymaps.yaml)
to `/config/bluetooth_hid_remote_keymaps.yaml`, edit it, then reopen the
integration's Configure dialog and select the profile. The file is optional.

```yaml
profiles:
  my_android_remote:
    extends: android_tv
    namespace: android
    mappings:
      "07:0058": DPAD_CENTER
      "0C:008D": GUIDE
      "0C:0223": HOME
```

Mapping keys are hexadecimal `HID Usage Page:HID Usage ID` pairs. Android
profile values are names from Android's `KeyEvent` API, optionally prefixed by
`KEYCODE_`. A mapping may instead contain both `key_code` and `key_name`; the
integration rejects Android name/code mismatches. HID custom profiles inherit
the comprehensive HID names and may override a display name while retaining
the numeric HID Usage ID.

Device-specific branded buttons generally belong in a custom profile. The
exception currently promoted into Android TV / Fire TV is vendor Usage Page
`0x00FF`, Usage IDs `0x00A1` through `0x00A4`: identical reports were verified
on the genuine Fire TV remote and the separately manufactured compatible AR.
They intentionally use Android's service-neutral `VIDEO_APP_1` through
`VIDEO_APP_4` identities rather than baking streaming-service brands into the
integration.

## Voice assistant

A voice-capable remote creates an **Assist** entity and an **Assist pipeline**
select entity in addition to its button event and diagnostic entities. Choose
the pipeline on that select entity. In the integration's Configure dialog,
**Voice assistant** can optionally select a `media_player` for spoken replies;
leaving it empty runs STT and intent handling without generating TTS.

- On Fire TV remotes, hold the Search/voice button while speaking and release
  it to submit the utterance. Genuine Fire TV hardware and the tested `AR`
  compatible remote use independently verified HOGP/Opus control behavior.
- On Google TV remotes, press Search/voice once and speak during the bounded
  five-second ATVV capture. Genuine and tested compatible remotes use the same
  verified ATVV transport.

Audio is kept only in bounded memory for the active utterance, decoded locally,
and sent to the selected Home Assistant Assist pipeline starting at STT. The
integration does not persist raw recordings and does not implement a wake word.
Capture also stops on transport end, the remote's release where applicable, or
a 15-second safety timeout. If the Assist entities do not appear, BlueZ did not
expose a supported voice transport for that device.

## Installation

Home Assistant **2026.8.1 or newer** is required. Add this repository to HACS
as a custom **Integration** repository. Tagged
releases are installed through HACS in the usual way; `main` may contain
unreleased hardware-validation work.

After installation and a Home Assistant restart, put a BLE HOGP remote in
pairing mode and add **Bluetooth HID Remote** from Settings > Devices & services.
Select the visible remote and confirm pairing. The integration registers a
temporary BlueZ agent bound to that exact device, verifies the resulting HOGP
bond, marks it trusted, and then leaves all future HID connection handling to
BlueZ's built-in `input-hog` profile.

For a dual-mode device that exposes BlueZ's optional `PreferredBearer`
property, the flow selects `le` immediately before pairing. This prevents a
BLE HOGP remote from falling back to an unrelated BR/EDR page attempt. The
integration still does not call `Connect` or own the later HID connection.

If a remote is factory-reset while BlueZ still retains its old bond, the first
confirmation detects that unusable bond and opens a separate replacement
confirmation. Only after that explicit confirmation does the integration call
BlueZ `RemoveDevice` for the selected remote, wait for the same address to be
rediscovered in pairing mode, and create a fresh bond. Other paired keyboards,
mice, and remotes are not changed.

If pairing, bonding, service resolution, and the advertised HOGP UUID are all
complete but BlueZ has not exported the concrete GATT service objects, the
flow preserves that complete bond and opens a recovery step. It does not create
an integration entry until a physical wake/reconnection makes the actual HOGP
service available. A failed recovery leads to the separate, explicit bond
replacement confirmation rather than deleting the bond automatically.

One tested remote identifies itself as `AR`, appearance `0x0180`, advertises
HOGP, and carries the observed manufacturer/product fingerprint
`0x0171:041e`, but returns a malformed characteristic-discovery response that
BlueZ cannot parse. After this exact device is paired and bonded, the flow
offers a separate compatibility confirmation. On HAOS only, accepting it
briefly stops the host Bluetooth service, snapshots and replaces only that
device's non-secret GATT cache with the verified table bundled in this
integration, then restarts Bluetooth and verifies the real HOGP objects. The
device's bond keys are stored separately and are neither read nor modified.
The repair is never offered unless the name, appearance, advertised HOGP UUID,
manufacturer/product fingerprint, and bond state all match.

If service verification still fails, the same recovery screen offers an
explicit rollback. It restores the exact pre-repair cache snapshot (or removes
the newly created cache when none existed) and aborts setup without creating an
integration entry.

This compatibility action temporarily disconnects every host-Bluetooth device.
It is intentionally not automatic, does not change BlueZ configuration or its
built-in `input-hog` profile, and is unsupported outside Home Assistant OS.

## Best-effort console input protection

BlueZ's built-in HOGP profile normally creates Linux `/dev/input/event*`
devices. Without exclusive ownership, key presses can reach the active HAOS
host console as well as this integration's GATT event entity. Each configured
entry therefore acquires `EVIOCGRAB` on every event node whose sysfs `uniq`
value exactly matches that entry's Bluetooth address. It never selects by name,
vendor, event number, or merely being a Bluetooth device, so unrelated local
keyboards and mice remain untouched.

The diagnostic **Console input protection** binary sensor is on only when at
least one matching event node exists and every matching node is held. Its
attributes list matching nodes, acquired nodes, and any open/grab failures.
The integration continuously reconciles node creation and removal because
BlueZ can assign new event numbers after a reconnect. Entry unload, reload,
setup failure, and Core shutdown close every descriptor; the kernel also
releases ownership if Core exits unexpectedly.

This mechanism is deliberately fail-open: a remote entry can still provide HA
events when an event node cannot be opened or `EVIOCGRAB` fails. In that case
the diagnostic sensor is off and its attributes contain the exact failed node
and error. A newly recreated node is discovered by a 250 ms reconciliation
loop, so a short reconnect window can exist before the grab is acquired. Do
not treat this HACS integration as an absolute guarantee that no keystroke can
reach the host console.

This protection begins when Home Assistant Core loads the config entry. It
cannot cover the earlier HAOS boot interval before Core and the integration are
running. Eliminating that interval would require a host-level udev or system
service rather than a portable HACS integration.

A directly attached Bluetooth adapter is required. ESPHome Bluetooth proxies
can advertise discovery data to Home Assistant, but cannot perform this pairing
or transport the operating-system HID profile used by the integration.

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
