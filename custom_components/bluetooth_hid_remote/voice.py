"""Bounded Opus voice transport helpers for HID remotes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from struct import pack
from typing import Final

VOICE_REPORT_ID: Final = 0xF0
VOICE_PACKET_SIZE: Final = 80
VOICE_PACKET_TOC: Final = 0xB8
VOICE_FRAME_SAMPLES_48KHZ: Final = 960
VOICE_SAMPLE_RATE: Final = 16_000
MAX_VOICE_PACKETS: Final = 750
MAX_PCM_BYTES: Final = VOICE_SAMPLE_RATE * 2 * 20
FFMPEG_TIMEOUT: Final = 20.0


class VoiceTransportError(RuntimeError):
    """Raised when a remote voice stream cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class HidVoicePacket:
    """One validated Opus packet received through a HID input report."""

    report_id: int
    characteristic_handle: int
    data: bytes


@dataclass(slots=True)
class OpusVoiceBuffer:
    """Collect one bounded push-to-talk utterance."""

    max_packets: int = MAX_VOICE_PACKETS
    packets: list[bytes] = field(default_factory=list)

    def append(self, packet: bytes | bytearray) -> None:
        """Validate and append one packet, rejecting unbounded recordings."""
        value = bytes(packet)
        if not is_supported_opus_packet(value):
            raise VoiceTransportError("unsupported HID Opus packet")
        if len(self.packets) >= self.max_packets:
            raise VoiceTransportError("HID voice recording exceeded its packet limit")
        self.packets.append(value)

    def to_ogg(self) -> bytes:
        """Wrap the captured packets in a standards-compliant Ogg stream."""
        if not self.packets:
            raise VoiceTransportError("HID voice recording is empty")
        return build_ogg_opus(self.packets)


def is_supported_opus_packet(data: bytes | bytearray) -> bool:
    """Recognize the fixed-size mono Opus framing observed on BLE remotes."""
    if len(data) != VOICE_PACKET_SIZE or data[0] != VOICE_PACKET_TOC:
        return False
    toc = data[0]
    config = toc >> 3
    stereo = (toc >> 2) & 1
    frame_code = toc & 0x03
    return config == 23 and stereo == 0 and frame_code == 0


def build_ogg_opus(packets: list[bytes]) -> bytes:
    """Build an Ogg Opus byte stream from 20 ms mono packets."""
    if not packets:
        raise VoiceTransportError("cannot build an empty Ogg stream")
    if len(packets) > MAX_VOICE_PACKETS:
        raise VoiceTransportError("too many Opus packets")
    if not all(is_supported_opus_packet(packet) for packet in packets):
        raise VoiceTransportError("invalid Opus packet in recording")

    serial = 0x42544852  # Stable local stream serial: "BTHR".
    vendor = b"Bluetooth HID Remote"
    opus_head = b"OpusHead" + pack("<BBHIhB", 1, 1, 0, VOICE_SAMPLE_RATE, 0, 0)
    opus_tags = b"OpusTags" + pack("<I", len(vendor)) + vendor + pack("<I", 0)
    pages = [
        _ogg_page(opus_head, serial, sequence=0, granule=0, flags=0x02),
        _ogg_page(opus_tags, serial, sequence=1, granule=0, flags=0),
    ]
    granule = 0
    for index, packet in enumerate(packets, start=2):
        granule += VOICE_FRAME_SAMPLES_48KHZ
        flags = 0x04 if index == len(packets) + 1 else 0
        pages.append(_ogg_page(packet, serial, index, granule, flags))
    return b"".join(pages)


def _ogg_page(
    payload: bytes, serial: int, sequence: int, granule: int, flags: int
) -> bytes:
    segments = []
    remaining = len(payload)
    while remaining >= 255:
        segments.append(255)
        remaining -= 255
    segments.append(remaining)
    segment_table = bytes(segments)
    header = (
        b"OggS"
        + bytes((0, flags))
        + pack("<QII", granule, serial, sequence)
        + b"\x00\x00\x00\x00"
        + bytes((len(segment_table),))
        + segment_table
    )
    page = header + payload
    checksum = _ogg_crc(page)
    return page[:22] + pack("<I", checksum) + page[26:]


def _ogg_crc(data: bytes) -> int:
    checksum = 0
    for value in data:
        checksum ^= value << 24
        for _ in range(8):
            checksum = (
                ((checksum << 1) ^ 0x04C11DB7)
                if checksum & 0x80000000
                else checksum << 1
            ) & 0xFFFFFFFF
    return checksum


async def async_decode_opus_packets(
    ffmpeg_binary: str,
    packets: list[bytes],
    *,
    timeout: float = FFMPEG_TIMEOUT,
) -> bytes:
    """Decode one bounded HID Opus utterance to Assist-compatible PCM."""
    ogg = build_ogg_opus(packets)
    process = await asyncio.create_subprocess_exec(
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "ogg",
        "-i",
        "pipe:0",
        "-f",
        "s16le",
        "-ac",
        "1",
        "-ar",
        str(VOICE_SAMPLE_RATE),
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(ogg), timeout)
    except (TimeoutError, asyncio.CancelledError):
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        message = stderr.decode(errors="replace").strip()
        raise VoiceTransportError(f"ffmpeg could not decode HID voice: {message}")
    if not stdout:
        raise VoiceTransportError("ffmpeg decoded an empty HID voice stream")
    if len(stdout) > MAX_PCM_BYTES:
        raise VoiceTransportError("decoded HID voice exceeded its size limit")
    return stdout
