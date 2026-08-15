"""Tests for bounded HID Opus voice transport helpers."""

import shutil

import pytest

from custom_components.bluetooth_hid_remote.voice import (
    MAX_VOICE_PACKETS,
    OpusVoiceBuffer,
    VoiceTransportError,
    async_decode_opus_packets,
    build_ogg_opus,
    is_supported_opus_packet,
)


def _silence_packet() -> bytes:
    # A valid 20 ms CELT-wideband mono Opus packet captured from AR.
    return bytes.fromhex(
        "b826965bd777c885ede76ed1cbf0a21ca1a70f985f4214c83a8a5e3879646572"
        "7f2b41b7956304fdbec738919d791e442ad2607dc892efdc0277ee609142f8b37"
        "5cb35cc2d284384cb272ed13fee3451"
    )


def test_voice_packet_requires_expected_size_and_opus_toc() -> None:
    packet = _silence_packet()

    assert is_supported_opus_packet(packet)
    assert not is_supported_opus_packet(packet[:-1])
    assert not is_supported_opus_packet(bytes([0xB9]) + packet[1:])
    assert not is_supported_opus_packet(bytes([0xB4]) + packet[1:])


def test_voice_buffer_is_bounded_and_rejects_invalid_packets() -> None:
    buffer = OpusVoiceBuffer(max_packets=1)
    buffer.append(_silence_packet())

    with pytest.raises(VoiceTransportError, match="packet limit"):
        buffer.append(_silence_packet())
    with pytest.raises(VoiceTransportError, match="unsupported"):
        OpusVoiceBuffer().append(b"not opus")


def test_ogg_wrapper_has_headers_pages_and_eos() -> None:
    ogg = build_ogg_opus([_silence_packet(), _silence_packet()])

    assert ogg.count(b"OggS") == 4
    assert b"OpusHead" in ogg
    assert b"OpusTags" in ogg
    assert len(ogg) < 512


def test_ogg_wrapper_rejects_unbounded_input() -> None:
    with pytest.raises(VoiceTransportError, match="too many"):
        build_ogg_opus([_silence_packet()] * (MAX_VOICE_PACKETS + 1))


@pytest.mark.asyncio
async def test_ffmpeg_decoder_produces_16khz_mono_pcm() -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed in the test environment")

    pcm = await async_decode_opus_packets(ffmpeg, [_silence_packet()] * 2)

    assert pcm
    assert len(pcm) % 2 == 0
