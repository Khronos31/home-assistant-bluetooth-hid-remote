"""Tests for the per-remote Assist pipeline selector."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.bluetooth_hid_remote import select as select_platform


@pytest.mark.asyncio
async def test_pipeline_select_is_added_after_late_capability_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sleeping remote can expose its selector after metadata arrives."""
    listener = None

    class Manager:
        supports_voice = False
        address = "00:11:22:33:44:55"
        name = "Remote"

        def async_add_voice_support_listener(self, new_listener):
            nonlocal listener
            listener = new_listener
            return Mock()

    manager = Manager()
    entry = SimpleNamespace(runtime_data=manager, async_on_unload=Mock())
    async_add_entities = Mock()
    hass = Mock()
    entity = object()
    monkeypatch.setattr(
        select_platform,
        "BluetoothHidRemotePipelineSelect",
        Mock(return_value=entity),
    )

    await select_platform.async_setup_entry(hass, entry, async_add_entities)
    async_add_entities.assert_not_called()

    manager.supports_voice = True
    assert listener is not None
    listener()
    listener()

    async_add_entities.assert_called_once()
    assert async_add_entities.call_args.args[0] == [entity]
