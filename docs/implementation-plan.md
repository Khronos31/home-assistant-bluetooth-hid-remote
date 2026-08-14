# Bluetooth HID Remote implementation plan

## Objective

Create a public HACS custom integration that discovers and pairs BLE
HID-over-GATT (HOGP) remotes using a directly attached Home Assistant OS
Bluetooth adapter and exposes each remote as a Home Assistant device with an
event entity.

## Acceptance criteria

1. `python -m pytest` passes tests for HID report parsing, config flow behavior,
   pairing-agent target restrictions, and event dispatch.
2. `ruff check .` and `ruff format --check .` pass.
3. HACS and Home Assistant manifest validation run in GitHub Actions.
4. The paired `AR` remote's Report Map can be read and a real button press
   updates its event entity without a Linux `/dev/input` device.
5. A workflow-dispatched release synchronizes `VERSION` and `manifest.json`,
   validates the tree, creates an annotated tag, and publishes a GitHub Release.

## Non-goals

- Bluetooth Classic HID.
- Initial pairing through an ESPHome Bluetooth Proxy.
- Modifying HAOS, BlueZ, or its built-in HOG profile.
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

1. Prove direct GATT Report Map access and Report notifications on the paired
   `AR` remote.
2. Implement descriptor-driven HID report decoding with unit tests.
3. Implement discovery, guarded BlueZ pairing, connection management, and the
   event entity.
4. Add HACS metadata, documentation, validation CI, and gated release CI.
5. Deploy locally, run `ha core check`, obtain restart approval, and verify a
   physical button event before creating a stable release.
