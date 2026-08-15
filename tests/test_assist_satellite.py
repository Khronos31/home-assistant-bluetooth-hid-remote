"""Tests for the push-to-talk Assist satellite state machine."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Home Assistant installs these Assist dependencies at runtime. The repository's
# minimal Python test environment does not compile their native extensions.
_micro_vad = ModuleType("pymicro_vad")
_micro_vad.MicroVad = object
_speex = ModuleType("pyspeex_noise")
_speex.AudioProcessor = object
sys.modules.setdefault("pymicro_vad", _micro_vad)
sys.modules.setdefault("pyspeex_noise", _speex)

from homeassistant.components.assist_pipeline import PipelineStage  # noqa: E402
from homeassistant.components.assist_satellite.entity import (  # noqa: E402
    AssistSatelliteState,
)

from custom_components.bluetooth_hid_remote.assist_satellite import (  # noqa: E402
    BluetoothHidRemoteAssistSatellite,
)
from custom_components.bluetooth_hid_remote.const import (  # noqa: E402
    CONF_VOICE_RESPONSE_PLAYER,
)
from custom_components.bluetooth_hid_remote.hid import HidUsage  # noqa: E402
from custom_components.bluetooth_hid_remote.manager import HidInputReport  # noqa: E402
from custom_components.bluetooth_hid_remote.voice import (  # noqa: E402
    HidVoicePacket,
    OpusVoiceBuffer,
    PcmVoiceBuffer,
    PcmVoicePacket,
)


def _voice_packet() -> bytes:
    return bytes.fromhex(
        "b826965bd777c885ede76ed1cbf0a21ca1a70f985f4214c83a8a5e3879646572"
        "7f2b41b7956304fdbec738919d791e442ad2607dc892efdc0277ee609142f8b37"
        "5cb35cc2d284384cb272ed13fee3451"
    )


def _new_entity(options: dict | None = None):
    manager = SimpleNamespace(
        address="00:11:22:33:44:55", name="Remote", key_mapper=object()
    )
    entry = SimpleNamespace(
        runtime_data=manager,
        options=options or {},
        async_create_background_task=lambda _hass, coro, **_kwargs: asyncio.create_task(
            coro
        ),
    )
    entity = BluetoothHidRemoteAssistSatellite(entry)
    entity.hass = SimpleNamespace(loop=asyncio.get_running_loop())
    return entity


@pytest.mark.asyncio
async def test_search_release_runs_one_bounded_voice_recording() -> None:
    """Search press/audio/release creates one and only one Assist task."""
    entity = _new_entity()
    states = []
    entity._set_state = states.append
    process = AsyncMock()
    entity._async_process_recording = process
    search = HidUsage(0x0C, 0x0221, "AC Search")

    entity._receive_report(HidInputReport(2, 33, b"\x21\x02", (search,)))
    entity._receive_voice_packet(HidVoicePacket(0xF0, 101, _voice_packet()))
    entity._receive_report(HidInputReport(2, 33, b"\x00\x00", (search,)))
    await asyncio.sleep(0)

    recording = process.await_args.args[0]
    assert isinstance(recording, OpusVoiceBuffer)
    assert recording.packets == [_voice_packet()]
    assert states[0] is AssistSatelliteState.LISTENING


@pytest.mark.asyncio
async def test_atvv_pcm_finishes_on_transport_stop() -> None:
    """An ATVV PCM stream starts after instant Search release and stops once."""
    entity = _new_entity()
    entity._set_state = lambda _state: None
    process = AsyncMock()
    entity._async_process_recording = process
    search = HidUsage(0x0C, 0x0221, "AC Search")

    entity._receive_report(HidInputReport(2, 33, b"\x21\x02", (search,)))
    entity._receive_report(HidInputReport(2, 33, b"\x00\x00", (search,)))
    entity._receive_voice_packet(PcmVoicePacket(16_000, b"\x01\x00\x02\x00"))
    entity._receive_voice_packet(None)
    await asyncio.sleep(0)

    recording = process.await_args.args[0]
    assert isinstance(recording, PcmVoiceBuffer)
    assert recording.to_pcm() == b"\x01\x00\x02\x00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "end_stage"),
    [
        ({}, PipelineStage.INTENT),
        (
            {CONF_VOICE_RESPONSE_PLAYER: "media_player.study"},
            PipelineStage.TTS,
        ),
    ],
)
async def test_response_player_controls_pipeline_end_stage(
    monkeypatch: pytest.MonkeyPatch, options: dict, end_stage: PipelineStage
) -> None:
    """No player skips TTS; a selected player requests a TTS result."""
    entity = _new_entity(options)
    entity.async_accept_pipeline_from_satellite = AsyncMock()
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.assist_satellite.get_ffmpeg_manager",
        lambda _hass: SimpleNamespace(binary="ffmpeg"),
    )
    monkeypatch.setattr(
        "custom_components.bluetooth_hid_remote.assist_satellite.async_decode_opus_packets",
        AsyncMock(return_value=b"pcm"),
    )

    buffer = OpusVoiceBuffer()
    buffer.append(_voice_packet())
    await entity._async_process_recording(buffer)

    call = entity.async_accept_pipeline_from_satellite.await_args
    assert call.kwargs["start_stage"] is PipelineStage.STT
    assert call.kwargs["end_stage"] is end_stage
    stream = call.args[0]
    assert [chunk async for chunk in stream] == [b"pcm"]


@pytest.mark.asyncio
async def test_pcm_recording_bypasses_ffmpeg() -> None:
    """Already-decoded ATVV PCM is passed directly to Assist."""
    entity = _new_entity()
    entity.async_accept_pipeline_from_satellite = AsyncMock()
    buffer = PcmVoiceBuffer()
    buffer.append(PcmVoicePacket(16_000, b"\x01\x00\x02\x00"))

    await entity._async_process_recording(buffer)

    stream = entity.async_accept_pipeline_from_satellite.await_args.args[0]
    assert [chunk async for chunk in stream] == [b"\x01\x00\x02\x00"]
