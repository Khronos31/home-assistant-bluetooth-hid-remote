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
5. Existing version-1 entries migrate to Android TV / Fire TV; new entries use
   the same remote-oriented default. HID and Google TV remain explicit options.
   Changing the option reloads only that config entry.
6. The local brand asset is byte-identical to the Home Assistant Bluetooth
   integration's served 256x256 PNG.
7. Android TV resolves the AR remote's legacy Keyboard/Keypad usage
   `0x0007:0x00F1` through Linux `KEY_BACK` and AOSP `Generic.kl` to Android
   `BACK`/`4`; the HID profile keeps code `241` and `HID_0007_00F1`.
8. A BlueZ input Report value cached before `StartNotify` is suppressed only
   when the first notification is byte-identical. A different first value is
   published as a real key press without delay.
9. Android TV / Fire TV maps the branded usages shared by the genuine Fire TV
   remote and compatible AR, `0x00FF:0x00A1` through `0x00A4`, to Android's
   service-neutral `VIDEO_APP_1`/`289` through `VIDEO_APP_4`/`292`.
10. The public Google TV profile maps all fifteen proprietary low-numbered
    usages observed identically on the genuine and compatible Google TV
    remotes into the Android namespace, while unobserved standard usages fall
    back to Android TV.
11. A sleeping voice remote need not expose its Report Map while Core forwards
    entity platforms. When metadata later proves voice support, the existing
    config entry adds its Assist satellite and pipeline selector exactly once;
    a non-voice remote still gains neither entity. If BlueZ reports the
    connection before its GATT objects are readable, one single-flight retry
    task probes the existing connection after 1, 2, 4, 8, and 16 seconds. It
    never opens a BLE connection and is cancelled on disconnect or unload.

### 2026-08-16 hardware validation

- The genuine and tested compatible Google TV remotes produced the same
  fifteen button usages and both completed the ATVV audio path. The Google TV
  profile is therefore public rather than a household-only custom profile.
- The genuine Fire TV remote and compatible AR produced the same ordinary key
  usages, including vendor usages `0x00FF:0x00A1` through `0x00A4`. Those keys
  belong in the public Android TV / Fire TV profile.
- Compatible AR starts sending 80-byte HID input Report `0xF0` packets while
  its microphone button is held. The genuine Fire TV remote instead requires
  the Amazon audio-state pair, F2 start `0x01` and stop `0x00`; after that
  handshake it also produced 80-byte `0xF0` Opus packets and completed Assist.
- All four independent hardware cells completed both ordinary-key and voice
  validation: genuine and compatible Google TV remotes, plus genuine and
  compatible Fire TV remotes.

### 2026-08-18 Google TV keycode correction

- Button Mapper and QuickBars running on the Google TV itself reported the
  three programmable buttons as `BUTTON_3`/`190`, `BUTTON_4`/`191`, and
  `MACRO_1`/`313`. The Google TV profile previously named the same usages
  `VIDEO_APP_1`, `VIDEO_APP_2`, and `BOOKMARK`, which no app on the TV ever
  sees. Usages `0x000C:0x000A`, `0x000C:0x000B`, and `0x000C:0x000D` now carry
  the codes the platform actually delivers.
- The Android TV / Fire TV vendor-page mappings `0x00FF:0x00A1` through
  `0x00A4` are unaffected; that hardware genuinely reports the `VIDEO_APP_*`
  identities.

## Voice remote roadmap

The v0.2.0 hardware coverage matrix contains four independent devices: genuine
and compatible Fire TV remotes, and genuine and compatible Google TV remotes.
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

### v0.2.0: Fire TV and Google TV remotes as Assist microphones

- The release target requires all four physical devices. The inexpensive `AR`
  remote and each genuine or compatible remote are separate compatibility
  claims; passing one never substitutes for another.
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

#### Bond removal and recovery contract

- Provide a confirmed **Unpair / rebuild bond** action from the integration's
  configuration flow. It must be the supported equivalent of
  `bluetoothctl remove <address>` and operate only on the address belonging to
  that config entry.
- Do not expose destructive bond removal as a normal `button` entity: Home
  Assistant button entities provide no integration-owned confirmation step,
  and leaving the config entry loaded after an accidental press creates an
  ambiguous disconnected state.
- Before removing a bond, stop the active Assist run, discard bounded audio
  buffers, release notification subscriptions and exclusive input grabs, then
  ask BlueZ to remove exactly the selected device. Never restart Bluetooth or
  remove unrelated bonds.
- After explicit removal, continue directly into the existing pairing flow so
  the user can rebuild the bond without opening a host console. A failed or
  cancelled re-pair must remain recoverable by reopening the same flow.
- Config-entry deletion must also remove that entry's BlueZ bond after an
  explicit confirmation in the UI. If Home Assistant's entry-removal lifecycle
  cannot present that confirmation reliably, retain the dedicated unpair flow
  as a required pre-delete action and surface a repair explaining it; do not
  silently leave a console-active orphaned HID bond.

#### Assist configuration contract

- Create one `assist_satellite` entity for each voice-capable remote. Voice
  support belongs to that remote's existing Home Assistant device; it is not a
  global integration-wide microphone.
- Create a per-remote `select` entity containing the Assist pipelines currently
  configured in Home Assistant. Expose that entity through the satellite's
  `pipeline_entity_id`, so the user selects the assistant by its displayed
  pipeline name rather than entering or copying an internal pipeline ID.
- Read and update pipeline choices through Home Assistant's supported Assist
  APIs. Do not read or modify `.storage` and do not hard-code the preferred
  pipeline.
- Start at the STT stage because the physical voice button is the activation
  mechanism; no wake-word pass is needed. The button press starts one run and
  release closes its audio input.
- Offer a per-remote response policy. The minimum choices are no spoken
  response (run through intent handling) and TTS playback on a selected
  `media_player`. A remote without a speaker must never leave a satellite in a
  responding state waiting for playback that cannot finish.
- Pipeline selection and response routing are independent. Different remotes
  may use different Assist pipelines and different response players.

#### AR voice-transport evidence (2026-08-15)

- The tested `AR` sends Consumer Control `AC Search` in Report ID `0x02` when
  its voice button is pressed and sends the all-zero release report when the
  button is released.
- While held, input Report ID `0xF0` on observed characteristic handle 101
  carries one 80-byte Opus packet per notification. Handle numbers are runtime
  observations and must not be used as a protocol identifier.
- Every captured audio packet began with Opus TOC byte `0xB8`: configuration
  23, CELT-only wideband, mono, one 20 ms frame. Wrapping 94 captured packets
  in Ogg Opus decoded without an ffmpeg codec error to 1.88 seconds of audible
  16 kHz mono PCM.
- Notification timing contained gaps from roughly 40 ms through 518 ms. The
  current evidence does not distinguish remote-side discontinuous/lost
  transmission from loss in the diagnostic entity/WebSocket path. Production
  voice handling must consume the manager's raw notification callback, bound
  queues, and apply an explicit packet-loss policy rather than using event
  entity state as an audio transport.
- This proves that the integration can recover microphone audio from the exact
  tested `AR` variant, which is the compatible-Fire cell in the v0.2.0 matrix.
  It does not imply support for other Fire-style look-alikes; framing,
  transport stability, and start/stop behavior must also be repeated with the
  genuine Fire TV remote.

#### Implemented pre-hardware slice (unreleased, 2026-08-15)

- `async_remove_hogp_device` removes one exact BlueZ device path. The options
  flow unloads the entry, rebuilds only that bond, and restores protected input
  handling. If entry setup fails after pairing, it removes the new bond again.
  Config-entry deletion also removes only its own bond.
- Report ID `0xF0` is classified as voice only when the parsed HID descriptor
  declares an 80-byte input payload and the packet carries the observed mono
  20 ms Opus TOC (`0xB8`). A report ID alone never suppresses a key event.
- Valid voice packets bypass the event entity, Last key sensors, and Recorder.
  Capture is limited to 750 packets / 15 seconds and is closed on key release,
  disconnect, or unload.
- Captured Opus is wrapped in memory as Ogg, decoded by Home Assistant's ffmpeg
  binary to 16 kHz, signed 16-bit, mono PCM, then sent to Assist at the STT
  stage. No raw recording is persisted by the integration.
- Voice-capable remotes gain one Assist satellite and one pipeline selector on
  the existing device. With no response player configured, the run ends at
  intent handling; with a player configured, TTS is sent to that player and
  the satellite is explicitly returned to idle.
- This is implementation evidence only. Release, version changes, tags, and
  compatibility claims remain blocked until the four-device matrix passes.

#### Genuine Google TV Remote protocol research (2026-08-15)

- The household's genuine `Google TV Remote` is bonded at
  `C4:19:D1:5E:6C:C3`. Its voice button produces an immediate HID Search
  press/release rather than holding the key for the duration of speech. It
  therefore cannot use the AR's release-delimited HOGP transport unchanged.
- Google's open [Android TV remote reference firmware][google-atvv-gatt]
  defines a separate ATVV
  service `ab5e0001-5a21-4f05-bc7d-af01f617b664`, with host command TX
  (`...0002...`), remote audio RX (`...0003...`), and remote control
  (`...0004...`) characteristics. Handles are intentionally not part of the
  contract.
- The published [ATVV control definitions][google-atvv-control] support
  capability negotiation (`0x0A`),
  microphone open (`0x0C`), close (`0x0D`), and timeout extension (`0x0E`).
  Remote control notifications distinguish Search (`0x08`), Audio Start
  (`0x04`), Audio Stop (`0x00`), and synchronization messages.
- Version 1.0 advertises 8/16 kHz ADPCM and interaction modes including
  on-request, press-to-talk, and hold-to-talk. This is materially different
  from AR's 80-byte Opus-over-HOGP reports.
- BlueZ exposed all three ATVV characteristics on the genuine remote. The
  observed handles were TX 54, Audio RX 56, and Control 59; these remain
  diagnostics only and the implementation selects characteristics by UUID.
- The host sent GET_CAPS `0A 01 00 00 03 00`. The remote replied
  `0B 01 00 03 00 00 C8 00 00`: ATVV 1.0, 8/16 kHz ADPCM, on-request
  interaction, and a 200-byte audio frame.
- Search (`0x08`) followed by host MIC_OPEN Capture (`0C 01`) produced Audio
  Start (`04 00 02 00`) and 200-byte Audio RX notifications. A bounded
  five-second run delivered 193--194 packets before host MIC_CLOSE (`0D 00`)
  and Audio Stop (`00 00`). The packet cadence and size account for 400 ADPCM
  samples / 25 ms per notification at 16 kHz.
- Codec `0x02` is the reference firmware's 16 kHz IMA ADPCM mode. It is a
  continuous stream initialized with predictor 0 and step index 1, with the
  first code in each byte's high nibble. Control sync `0x0A`, when present,
  supplies a replacement codec, sequence, signed predictor, and step index.
- The unreleased implementation decodes each bounded notification directly to
  16 kHz signed-16 mono PCM, keeps it outside event/entity state and logs, and
  feeds the same per-device Assist satellite used by the HOGP/Opus path. A
  genuine-device run recognized `今、何時ですか？` exactly and completed the
  Intent stage successfully.
- The separately purchased compatible Google TV remote reproduced the same
  ATVV behavior and completed Assist independently, so both Google hardware
  cells are validated rather than inferred from appearance.

#### Genuine Fire TV voice control research (unreleased, 2026-08-16)

- The genuine Fire TV remote and the tested compatible `AR` remote expose the
  same descriptor-level BSA voice layout: `0xF0` is an 80-byte Opus input,
  `0xF1` is voice-control input, and `0xF2` is a one-byte host-to-remote output.
- The compatible `AR` observed in this household starts sending `0xF0` packets
  by itself. Its descriptor matches the genuine remote, but that does not make
  their host-control state values interchangeable.
- The unreleased implementation locates output report `0xF2` through its HID
  Report Reference descriptor and characteristic write properties. It does not
  hard-code a GATT handle or object path.
- After a descriptor-decoded Search press, the manager waits 150 ms for a
  native `0xF0` packet. Only when none arrives does it write the BSA start
  command; it writes stop on release. This preserves the already-working
  self-starting compatible remote while enabling the genuine remote.
- A release racing the asynchronous start write is explicitly closed by an
  immediate stop, and disconnect/unload clears the per-device command state.
- Protocol and unit-test results were followed by independent Assist captures
  on both the genuine Fire remote and compatible `AR`.
- The first genuine-device run selected F2 correctly but a direct BlueZ GATT
  Write Request was rejected by the remote with ATT `Insufficient
  Authorization`. Linux had already bound the same address to its HOGP hidraw
  device. The next implementation therefore resolves `/dev/hidraw*` by the
  exact `HID_UNIQ` Bluetooth address and sends the numbered output report
  through the kernel HID driver first, with direct GATT retained only as a
  fallback. Volatile hidraw node numbers and product IDs are not part of the
  matching contract.
- A passive `btmon` capture then proved that the hidraw path produced an ATT
  Write Request to the genuine remote's F2 value handle and that the remote
  returned a successful Write Response. No `0xF0` audio followed `0x02`, so
  transport and authorization were no longer plausible causes.
- Amazon's published [Fire TV BLE remote kernel driver][amazon-fire-hid]
  defines the genuine Lab126 audio-state commands as start `0x01` and stop
  `0x00`. The earlier `0x02`/`0x03` values came from a generic BSA reference,
  not Amazon's host implementation. Product `0171:042F` now selects Amazon's
  pair by reading `HID_ID` from the exact address-matched hidraw sysfs node,
  with BlueZ `Modalias` retained as a fallback. The compatible remote keeps
  its existing self-start-first behavior and generic fallback values.
- Hardware validation on `0171:042F` then succeeded: F2 start `0x01` produced
  continuous 80-byte Report ID `0xF0` Opus notifications, F2 stop `0x00`
  closed the stream, and the per-device Assist satellite returned to idle
  after processing. The compatible `0171:041E` was tested again afterward;
  it continued to self-start its `0xF0` stream and completed Assist without
  receiving the genuine-device control pair. The Fire transport portion of
  the v0.2.0 hardware matrix is therefore complete.

[google-atvv-gatt]: https://android.googlesource.com/platform/hardware/telink/atv/refDesignRcu/+/refs/heads/master/vendor/827x_ble_remote/app_att.c
[google-atvv-control]: https://android.googlesource.com/platform/hardware/telink/atv/refDesignRcu/+/86f501098fb4ba60954cb046201ffe43ca360c3e/application/audio/gl_audio.h
[infineon-bsa-voice]: https://github.com/Infineon/mtb-example-btstack-freertos-cyw20829-voice-remote
[amazon-fire-hid]: https://github.com/amazon-oss/android_kernel_amazon_mt8695/blob/c256543c13d2e2f235aa1ae4562ff8724b90dab6/drivers/hid/hid-ftv-bleremote.c

### Completed v0.2.0 voice validation

- Genuine and compatible Google TV remotes both use the implemented ATVV
  transport and completed Assist independently.
- Genuine Fire TV uses Amazon's host-controlled HOGP/Opus state values;
  compatible `AR` uses its independently verified self-starting HOGP/Opus
  behavior.
- Keep device-specific transports behind one common push-to-talk/Assist
  session interface; do not leak Google- or Fire-TV-specific report handling
  into the event entity.
