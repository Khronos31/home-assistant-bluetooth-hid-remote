# Bluetooth HID Remote implementation plan

## Objective

Create a public HACS custom integration that discovers already-paired BLE
HID-over-GATT (HOGP) remotes using a directly attached Home Assistant OS
Bluetooth adapter and exposes each remote as a Home Assistant device with an
event entity.

## Acceptance criteria

1. `python -m pytest` passes tests for HID report parsing, passive connection
   behavior, notification ownership, and event dispatch.
2. `ruff check .` and `ruff format --check .` pass.
3. HACS and Home Assistant manifest validation run in GitHub Actions.
4. A real button press from the paired `AR` remote updates its event entity
   through BlueZ notifications without a Linux `/dev/input` device.
5. A workflow-dispatched release synchronizes `VERSION` and `manifest.json`,
   validates the tree, creates an annotated tag, and publishes a GitHub Release.

## Non-goals

- Bluetooth Classic HID.
- Initial pairing through an ESPHome Bluetooth Proxy.
- Pairing a remote or modifying HAOS, BlueZ, or its built-in HOG profile.
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
5. Deploy locally, run `ha core check`, obtain restart approval, and verify a
   physical button event before creating a stable release.

## Hardware-spike evidence

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
  responses. A pre-seeded BlueZ GATT cache was required for that device; this
  device-specific OS workaround is intentionally outside the integration.
