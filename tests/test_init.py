"""Tests for config-entry defaults and migration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.bluetooth_hid_remote import (
    async_migrate_entry,
    async_setup_entry,
)
from custom_components.bluetooth_hid_remote.compatibility import (
    CompatibilityRepairError,
)
from custom_components.bluetooth_hid_remote.config_flow import (
    BluetoothHidRemoteConfigFlow,
)
from custom_components.bluetooth_hid_remote.const import (
    CONF_ADDRESS,
    CONF_KEY_PROFILE,
    CONF_NAME,
    KEY_PROFILE_ANDROID_TV,
    KEY_PROFILE_HID,
)
from custom_components.bluetooth_hid_remote.pairing import (
    PairingRejectedError,
    PairingServicePendingError,
    PairingStaleBondError,
)


def _new_pairing_flow() -> BluetoothHidRemoteConfigFlow:
    """Create a directly testable flow with Home Assistant task semantics."""
    flow = BluetoothHidRemoteConfigFlow()
    flow.hass = SimpleNamespace(
        async_create_task=lambda target, **kwargs: asyncio.create_task(target)
    )
    flow._discovery = SimpleNamespace(address="00:11:22:33:44:55", name="Remote")
    flow._async_supports_compatibility_repair = AsyncMock(return_value=False)
    flow.context = {"title_placeholders": {"name": "Remote"}}
    return flow


async def _finish_pairing_progress(
    flow: BluetoothHidRemoteConfigFlow,
) -> dict:
    """Let a mocked pairing task finish and advance through progress results."""
    await asyncio.sleep(0)
    progress_done = await flow.async_step_pairing_progress()
    assert progress_done["type"] == "progress_done"
    assert progress_done["step_id"] == "pairing_result"
    return await flow.async_step_pairing_result()


@pytest.mark.asyncio
async def test_setup_acquires_input_before_forwarding_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Console protection starts before platform setup can delay the entry."""
    operations: list[str] = []
    manager = SimpleNamespace(
        async_start=AsyncMock(side_effect=lambda: operations.append("start")),
        async_stop=AsyncMock(),
    )
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.async_create_key_mapper",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.BluetoothHidRemoteManager",
        lambda *_args: manager,
    )

    async def forward(*_args) -> None:
        operations.append("forward")

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_forward_entry_setups=forward)
    )
    entry = SimpleNamespace(
        data={CONF_ADDRESS: "00:11:22:33:44:55", CONF_KEY_PROFILE: KEY_PROFILE_HID},
        options={},
        runtime_data=None,
        add_update_listener=lambda _listener: lambda: None,
        async_on_unload=lambda _unload: None,
    )

    assert await async_setup_entry(hass, entry) is True
    assert operations == ["start", "forward"]
    assert entry.runtime_data is manager


@pytest.mark.asyncio
async def test_new_entries_default_to_hid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A newly discovered remote starts in the standards-based HID profile."""
    pair = AsyncMock()
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    flow = _new_pairing_flow()
    flow.async_create_entry = lambda **kwargs: kwargs

    progress = await flow.async_step_bluetooth_confirm({})
    assert progress["type"] == "progress"
    assert progress["step_id"] == "pairing_progress"
    assert progress["progress_action"] == "pairing"
    result = await _finish_pairing_progress(flow)

    assert result["data"] == {
        CONF_ADDRESS: "00:11:22:33:44:55",
        CONF_KEY_PROFILE: KEY_PROFILE_HID,
        CONF_NAME: "Remote",
    }
    pair.assert_awaited_once_with("00:11:22:33:44:55")


@pytest.mark.asyncio
async def test_pairing_error_stays_in_confirmation_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected bond is rendered as a recoverable form error, not HTTP 500."""
    pair = AsyncMock(side_effect=PairingRejectedError)
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    flow = _new_pairing_flow()

    progress = await flow.async_step_bluetooth_confirm({})
    assert progress["type"] == "progress"
    result = await _finish_pairing_progress(flow)

    assert result["type"] == "form"
    assert result["step_id"] == "bluetooth_confirm"
    assert result["errors"] == {"base": "pairing_rejected"}


@pytest.mark.asyncio
async def test_stale_bond_requires_separate_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable existing bond is never replaced by the first confirmation."""
    pair = AsyncMock(side_effect=PairingStaleBondError)
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    flow = _new_pairing_flow()

    await flow.async_step_bluetooth_confirm({})
    result = await _finish_pairing_progress(flow)

    assert result["type"] == "form"
    assert result["step_id"] == "replace_bond"
    pair.assert_awaited_once_with("00:11:22:33:44:55")


@pytest.mark.asyncio
async def test_complete_bond_without_service_enters_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A complete bond is retained without creating an unusable entry."""
    pair = AsyncMock(side_effect=PairingServicePendingError)
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    flow = _new_pairing_flow()

    await flow.async_step_bluetooth_confirm({})
    result = await _finish_pairing_progress(flow)

    assert result["type"] == "form"
    assert result["step_id"] == "service_pending"


@pytest.mark.asyncio
async def test_ar_missing_service_offers_explicit_cache_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tested AR takes the opt-in cache path and keeps its completed bond."""
    pair = AsyncMock(side_effect=[PairingServicePendingError(), None])
    repair = AsyncMock()
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_install_ar_gatt_cache",
        repair,
    )
    flow = _new_pairing_flow()
    flow._async_supports_compatibility_repair = AsyncMock(return_value=True)
    flow.async_create_entry = lambda **kwargs: kwargs

    await flow.async_step_bluetooth_confirm({})
    repair_offer = await _finish_pairing_progress(flow)
    assert repair_offer["type"] == "form"
    assert repair_offer["step_id"] == "compatibility_repair"

    progress = await flow.async_step_compatibility_repair({})
    assert progress["progress_action"] == "repairing_compatibility"
    wake = await _finish_pairing_progress(flow)
    assert wake["type"] == "form"
    assert wake["step_id"] == "compatibility_wake"
    repair.assert_awaited_once_with(flow.hass, "00:11:22:33:44:55")

    await flow.async_step_compatibility_wake({})
    result = await _finish_pairing_progress(flow)
    assert result["data"][CONF_ADDRESS] == "00:11:22:33:44:55"
    assert pair.await_count == 2


@pytest.mark.asyncio
async def test_ar_cache_repair_failure_remains_on_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied or failed HAOS operation stays recoverable in the same form."""
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_install_ar_gatt_cache",
        AsyncMock(side_effect=CompatibilityRepairError("failed")),
    )
    flow = _new_pairing_flow()

    await flow.async_step_compatibility_repair({})
    result = await _finish_pairing_progress(flow)

    assert result["type"] == "form"
    assert result["step_id"] == "compatibility_repair"
    assert result["errors"] == {"base": "compatibility_repair_failed"}


@pytest.mark.asyncio
async def test_failed_service_recovery_offers_explicit_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second missing-service check does not delete the bond automatically."""
    pair = AsyncMock(side_effect=PairingServicePendingError)
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    flow = _new_pairing_flow()

    progress = await flow.async_step_service_pending({})
    assert progress["type"] == "progress"
    assert progress["progress_action"] == "verifying_service"
    result = await _finish_pairing_progress(flow)

    assert result["type"] == "form"
    assert result["step_id"] == "replace_bond"
    pair.assert_awaited_once_with("00:11:22:33:44:55")


@pytest.mark.asyncio
async def test_confirmed_stale_bond_replacement_creates_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedicated confirmation opts in to replacing only the selected bond."""
    pair = AsyncMock()
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    flow = _new_pairing_flow()
    flow.async_create_entry = lambda **kwargs: kwargs

    progress = await flow.async_step_replace_bond({})
    assert progress["type"] == "progress"
    assert progress["progress_action"] == "replacing_bond"
    result = await _finish_pairing_progress(flow)

    assert result["data"] == {
        CONF_ADDRESS: "00:11:22:33:44:55",
        CONF_KEY_PROFILE: KEY_PROFILE_HID,
        CONF_NAME: "Remote",
    }
    pair.assert_awaited_once_with("00:11:22:33:44:55", replace_existing=True)


@pytest.mark.asyncio
async def test_stale_bond_replacement_error_stays_on_replacement_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacement failures remain recoverable without hiding the destructive step."""
    pair = AsyncMock(side_effect=PairingRejectedError)
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    flow = _new_pairing_flow()

    await flow.async_step_replace_bond({})
    result = await _finish_pairing_progress(flow)

    assert result["type"] == "form"
    assert result["step_id"] == "replace_bond"
    assert result["errors"] == {"base": "pairing_rejected"}


@pytest.mark.asyncio
async def test_pairing_progress_does_not_start_a_second_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling a progress step never starts a second BlueZ pairing attempt."""
    release = asyncio.Event()

    async def wait_for_release(address: str) -> None:
        await release.wait()

    pair = AsyncMock(side_effect=wait_for_release)
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.config_flow.async_pair_hogp_device",
        pair,
    )
    flow = _new_pairing_flow()
    flow.async_create_entry = lambda **kwargs: kwargs

    first = await flow.async_step_bluetooth_confirm({})
    await asyncio.sleep(0)
    second = await flow.async_step_pairing_progress()

    assert first["type"] == "progress"
    assert second["type"] == "progress"
    assert first["progress_task"] is second["progress_task"]
    pair.assert_awaited_once_with("00:11:22:33:44:55")

    release.set()
    result = await _finish_pairing_progress(flow)
    assert result["data"][CONF_ADDRESS] == "00:11:22:33:44:55"


@pytest.mark.asyncio
async def test_version_one_entries_migrate_to_android_tv() -> None:
    """Existing installations preserve their pre-profile remote key behavior."""
    updates: list[dict] = []
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_update_entry=lambda entry, **kwargs: updates.append(kwargs)
        )
    )
    entry = SimpleNamespace(
        version=1,
        data={CONF_ADDRESS: "00:11:22:33:44:55", CONF_NAME: "Remote"},
    )

    assert await async_migrate_entry(hass, entry) is True
    assert updates == [
        {
            "data": {
                CONF_ADDRESS: "00:11:22:33:44:55",
                CONF_NAME: "Remote",
                CONF_KEY_PROFILE: KEY_PROFILE_ANDROID_TV,
            },
            "version": 2,
        }
    ]
