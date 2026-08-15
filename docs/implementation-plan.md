# Bluetooth HID Remote implementation plan

## Objective

Create a public HACS custom integration that discovers and pairs BLE
HID-over-GATT (HOGP) remotes using a directly attached Home Assistant OS
Bluetooth adapter and exposes each remote as a Home Assistant device with an
event entity.

## Acceptance criteria

1. `python -m pytest` passes tests for HID report parsing, passive connection
   behavior, notification ownership, profile mapping, sensors, and event
   dispatch.
2. `ruff check .` and `ruff format --check .` pass.
3. HACS and Home Assistant manifest validation run in GitHub Actions.
4. A real button press from the paired `AR` remote updates its event entity
   through BlueZ notifications without a Linux `/dev/input` device.
5. A workflow-dispatched release synchronizes `VERSION` and `manifest.json`,
   validates the tree, creates an annotated tag, and publishes a GitHub Release.
6. The config flow pairs an unbonded HOGP remote through a temporary BlueZ
   Agent1 bound to the explicitly selected device, verifies the bond and HOGP
   service, and removes incomplete state after failure.
7. If a reset remote leaves an unusable stale BlueZ bond, the config flow asks
   for a second explicit confirmation before replacing only that selected
   device's bond.
8. The exact tested `AR` identity may opt in to a bundled non-secret GATT-cache
   repair on HAOS; unrelated or unbonded HOGP devices can never enter it.
9. Every configured device exclusively acquires all Linux event nodes whose
   sysfs `uniq` exactly matches its Bluetooth address, releases them on unload,
   and publishes acquisition health as a diagnostic binary sensor.

## Non-goals

- Bluetooth Classic HID.
- Initial pairing through an ESPHome Bluetooth Proxy.
- PIN/passkey-entry pairing that requires typing on the remote.
- General-purpose Bluetooth keyboard support. A Just Works keyboard may be
  useful for validation, but keyboard-specific pairing and behavior are not a
  compatibility target.
- Mouse, pointer, touchpad, or motion input support. Such devices may pass the
  generic HOGP discovery and pairing path, but users are instructed not to add
  them and their resulting behavior is unsupported.
- Modifying BlueZ configuration or its built-in HOG profile. The narrowly
  scoped, confirmed `AR` cache repair described below is the only host-state
  exception.
- Stable-release claims for untested remote models.

## Constraints

- Do not modify `/config/secrets.yaml`, `/config/.ssh`, or `/config/.storage`.
- Do not modify unrelated dirty files in `/config` or other repositories.
- Run `ha core check` after deployment into `/config/custom_components`.
- Do not restart Home Assistant Core without explicit user approval.
- Pairing authorization must be limited to the device selected in the active
  config flow and removed on every exit path.

## Rollback

Remove the config entry and `/config/custom_components/bluetooth_hid_remote`,
then restart Home Assistant Core. The remote's existing BlueZ bond is left
unchanged unless the user explicitly removes it.

## Increments

1. Prove passive BlueZ Report notifications on the paired `AR` remote.
2. Implement raw HID report decoding with unit tests.
3. Implement discovery, passive BlueZ connection observation, notification
   ownership, and the event entity.
4. Add HACS metadata, documentation, validation CI, and gated release CI.
5. Add a target-bound temporary BlueZ pairing agent and clean failure rollback.
6. Deploy locally, run `ha core check`, obtain restart approval, and verify a
   physical button event before creating a stable release.

## Pairing increment

- Only direct BlueZ device objects are offered; proxy-only discoveries are
  rejected before the confirmation step.
- Each attempt opens a dedicated system D-Bus connection, exports one temporary
  `org.bluez.Agent1`, registers it with `DisplayYesNo` capability, and makes it
  the default agent for the lifetime of that dedicated pairing attempt.
- Agent callbacks authorize only the exact selected device path and only the
  HOGP service. PIN/passkey input requests and unrelated devices/services are
  rejected.
- Pairing calls `Device1.Pair` but never `Device1.Connect` or
  `Device1.Disconnect`, preserving BlueZ `input-hog` connection ownership.
- If BlueZ exposes `PreferredBearer` for a dual-mode discovery, the flow sets
  it to `le` before pairing. This keeps a BLE HOGP target from falling back to
  a BR/EDR page attempt without taking ownership of the HID connection.
- Watchers for `Paired`, `Bonded`, and `ServicesResolved` start before
  `Device1.Pair`, so a HID device that sleeps immediately cannot lose a
  transient success signal. A new entry is created only after all three were
  observed, the Device UUID list includes HOGP, and BlueZ exported an actual
  HOGP GATT service object. Trust is set only after that verification.
- A failed fresh attempt removes only the BlueZ device state created by that
  flow. Existing valid bonds are reused and never removed by the flow.
- An existing invalid bond raises a distinct stale-bond result. It is removed
  only from the dedicated replacement confirmation step, using the selected
  device's exact adapter and object path. The flow then waits for the same
  address to be rediscovered unpaired before starting a normal fresh attempt.
- A fresh attempt that reaches paired, bonded, services-resolved, and HOGP UUID
  state but lacks concrete HOGP GATT objects preserves its completed bond and
  enters a recovery step. No config entry is created until a physical wake
  exposes the real service. A second failed verification may offer the separate
  explicit replacement confirmation, but never removes the bond automatically.
- Agent unregistration, object unexport, and dedicated bus shutdown run on all
  exit paths.

## Console-input protection increment

- The selector is an exact, case-insensitive comparison between the configured
  Bluetooth address and `/sys/class/input/event*/device/uniq`. Names,
  vendor/product IDs, event numbers, and bus type do not broaden the match.
- All matching interfaces are acquired with Linux `EVIOCGRAB`; composite HID
  devices commonly expose separate keyboard and consumer-control event nodes.
- A bounded reconciliation worker detects disappearing and newly created event
  nodes. It explicitly ungrabs and closes stale descriptors, and retries failed
  acquisitions without repeatedly flooding the log with the same error.
- Manager startup acquires existing nodes before entity-platform forwarding.
  Failed setup and every unload path release all descriptors. Process exit also
  releases them through normal kernel file-descriptor cleanup.
- The diagnostic binary sensor is true only when at least one target node
  exists and all current target nodes are grabbed. Its attributes retain exact
  node and error details for runtime verification.
- Residual limitation: a HACS integration cannot protect the HAOS boot interval
  before Home Assistant Core loads the config entry.

## Tested AR compatibility increment

- Eligibility requires the exact observed identity (`Name=AR`, appearance
  `0x0180`, HOGP UUID, paired, and bonded) on a directly attached adapter.
- A separate confirmation explains that all host-Bluetooth devices will
  briefly disconnect. Generic HOGP devices never see this step.
- The bundled cache contains only the verified GATT attribute layout. It has no
  link keys, LTKs, IRKs, device secrets, or user credentials.
- The host helper validates exact adapter and device addresses, backs up only
  `/var/lib/bluetooth/<adapter>/cache/<device>`, replaces that one cache, and
  leaves the separately stored bond information untouched.
- Home Assistant stops `bluetooth.service`, waits for BlueZ to leave D-Bus,
  runs and verifies a bounded transient one-shot helper, then restarts BlueZ in
  a `finally` path. The config entry is still withheld until the repaired HOGP
  service objects are observable.

## Hardware-spike evidence

- The Home Assistant config flow completed first-time pairing without host
  console setup for an Ewin BLE keyboard and a PC120A BLE mouse. BlueZ exposed
  a 160-byte Report Map with three input characteristics for the keyboard and
  a 124-byte Report Map with two input characteristics for the mouse.
- Both generic devices delivered decoded event-entity updates after a Home
  Assistant Core restart, confirming persisted bonds, `input-hog` reconnect,
  notification resubscription, and config-entry restoration.
- The paired `AR` remote advertises HOGP and was discovered through the local
  Intel adapter.
- One real connection exposed a 149-byte Report Map and five input Report
  characteristics, proving that direct GATT access is possible on this HAOS
  host.
- Proactive reconnects to the sleeping remote raced BlueZ's built-in
  `input-hog` profile and produced repeated connection/service-discovery
  failures. Connection management was removed: BlueZ exclusively owns the
  HID connection, and the integration only observes its state and reports.
- The real wake connection is accepted directly by BlueZ's `input-hog`
  profile; Home Assistant's Bluetooth callback may receive no matching
  advertisement. The integration therefore uses Bleak's persistent BlueZ
  device watcher and never opens or closes the physical connection.
- A non-destructive open of `/dev/uhid` from the Studio Code Server add-on
  returned `EPERM`. That demonstrates the add-on device boundary only; it does
  not prove why the host `bluetoothd` process fails.
- The integration held BlueZ notification subscriptions for four input Report
  characteristics without owning the BLE connection.
- A real button press (`580000`) and release (`000000`) updated
  `event.ar_remote_button` and were persisted by Home Assistant Recorder.
- The same event path remained functional after restoring HAOS's default BlueZ
  configuration and restarting only the host Bluetooth service.
- The tested remote returns malformed mixed-width characteristic discovery
  responses. A clean config-flow pairing creates its bond but BlueZ exports no
  usable HOGP GATT service objects, while the generic keyboard and mouse work
  without a workaround. A pre-seeded BlueZ GATT cache was required for the AR;
  the integration now offers that exact non-secret cache as a separate,
  explicitly confirmed HAOS compatibility step.

## Key decoding increment

- Read the HID Report Map and each Report Reference through the existing BlueZ
  connection without changing connection ownership.
- Parse Input items for Report ID, bit offsets, field size/count, Usage Page,
  and array/variable usages.
- Preserve the raw event contract when metadata is unavailable or a usage is
  unknown.
- Resolve canonical usage names through the comprehensive HID Usage Tables
  database instead of maintaining a small integration-local lookup table.
- Preserve canonical `(hid_usage_page, hid_usage_id)` identity in every event
  and sensor regardless of the selected convenience profile.
- Offer a strict HID profile and an Android TV profile whose name and numeric
  code both come from Android `KeyEvent` constants; never mix HID, Linux evdev,
  and Android numeric namespaces.
- Load optional user profiles from
  `/config/bluetooth_hid_remote_keymaps.yaml`; validate the full file before
  reloading an entry and reject Android name/code mismatches.
- Carry decoded usages from a nonzero press into its following zero release.
- The AR fixture's real 149-byte Report Map decodes Report ID 1 payload
  `580000` as Keyboard/Keypad Usage `0x58` (`Keypad ENTER`). Home Assistant
  runtime validation confirmed both `0x58` and `0x51` on 2026-08-15, including
  matching key data on press and release events.
- Create Last key and Last key code sensor entities for the most recent decoded
  press. Both use the selected profile's one coherent name/code namespace,
  while attributes retain the canonical HID identity.

## v0.2.0 profile acceptance

1. HID `0x0007:0x0058` resolves to HID code `88` and `KEYPAD_ENTER` in the HID
   profile, and Android code `23` and `DPAD_CENTER` in the Android TV profile.
2. Android TV maps the observed Consumer usages `0x008D` and `0x0223` to
   `GUIDE`/`172` and `HOME`/`3` from the AOSP `KeyEvent` namespace.
   Keyboard/Keypad `0x0066` and Consumer `0x0030` both resolve to Android
   `POWER`/`26`, while retaining their distinct canonical HID identities.
3. Unknown Android mappings expose `UNKNOWN`/`0` without removing canonical HID
   attributes.
4. A custom YAML mapping can override a canonical usage, and malformed files,
   unknown Android names, and Android name/code mismatches are rejected.
5. Existing version-1 entries migrate to Android TV; new entries default to
   HID. Changing the option reloads only that config entry.
6. The local brand asset is byte-identical to the Home Assistant Bluetooth
   integration's served 256x256 PNG.
7. Android TV resolves the AR remote's legacy Keyboard/Keypad usage
   `0x0007:0x00F1` through Linux `KEY_BACK` and AOSP `Generic.kl` to Android
   `BACK`/`4`; the HID profile keeps code `241` and `HID_0007_00F1`.
8. A BlueZ input Report value cached before `StartNotify` is suppressed only
   when the first notification is byte-identical. A different first value is
   published as a real key press without delay.
9. A device-specific custom profile maps the AR remote's branded usages
   `0x00FF:0x00A1` through `0x00A4` to Android's service-neutral
   `VIDEO_APP_1`/`289` through `VIDEO_APP_4`/`292`; the built-in profile leaves
   these standard HID usages unknown.

## Voice remote roadmap

The hardware coverage matrix contains four independent devices: genuine and
compatible Fire TV remotes, then genuine and compatible Google TV remotes.
Passing one cell never implies support for another.

### Public key-profile contract

- Publish exactly three user-facing profile families: **HID**, **Android TV /
  Fire TV**, and **Google TV**.
- HID remains the strict canonical view. Android TV / Fire TV uses Android
  `KeyEvent` names and codes for standard and verified Fire-family reports.
  Google TV uses the same Android namespace but adds only mappings verified on
  genuine or compatible Google TV remotes.
- Key-profile selection affects presentation and key mapping only. Voice
  transport is detected independently from the actual GATT services and Report
  References. A look-alike remote may use generic Voice-over-HOGP even when its
  keys use the Google TV profile.
- Continue supporting validated custom YAML profiles as an advanced escape
  hatch. Promote a local mapping into a public profile only after a genuine or
  clearly attributable compatible device establishes its family provenance.
  The target household should not need local overrides once all four test
  remotes are covered by public profiles.

### v0.2.0: Fire TV Voice Remote as an Assist microphone

- The release target is a genuine Fire TV Voice Remote. The inexpensive `AR`
  remote is preliminary protocol evidence, not the compatibility claim.
- A voice-button press starts one push-to-talk Assist run; releasing it ends
  the input cleanly. Ordinary buttons continue to use the existing event
  entity contract.
- Classify HID input reports before dispatch. Voice payload reports must never
  become button events, Last key sensor updates, or Recorder attributes.
- Identify and test the remote's voice framing and codec, decode it into the
  audio format required by Home Assistant's Assist pipeline, and keep bounded
  buffering and session timeouts.
- Pairing, input grabbing, and unloading must also stop any active voice
  session and release its buffers. Raw microphone audio is neither retained
  nor exposed as entity state.
- Before release, removing an integration entry must remove only that entry's
  BlueZ bond, or offer an explicit equivalent unpair action. An orphaned bond
  must not block a later config-flow pairing or reconnect to the HAOS console
  without input protection.

### v0.3.0: Google TV Voice Remote support

- Add Assist input for a genuine Google TV Voice Remote after documenting its
  non-key GATT voice transport and host-side session control.
- Test the separately purchased Google TV-compatible remote independently.
  Its appearance and button layout do not imply protocol compatibility: record
  whether it reproduces Google's voice GATT services, uses generic
  Voice-over-HOGP reports like the `AR`, or supplies no usable microphone
  transport.
- Keep device-specific transports behind one common push-to-talk/Assist
  session interface; do not leak Google- or Fire-TV-specific report handling
  into the event entity.
- If the Google transport requires unavailable proprietary host components,
  document that hardware identity as unsupported rather than treating the
  look-alike remote as proof of compatibility.
