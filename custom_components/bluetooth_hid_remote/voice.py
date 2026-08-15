"""Bounded Opus voice transport helpers for HID remotes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from struct import pack, pack_into
from typing import Final

VOICE_REPORT_ID: Final = 0xF0
VOICE_PACKET_SIZE: Final = 80
VOICE_PACKET_TOC: Final = 0xB8
VOICE_FRAME_SAMPLES_48KHZ: Final = 960
VOICE_SAMPLE_RATE: Final = 16_000
MAX_VOICE_PACKETS: Final = 750
MAX_PCM_BYTES: Final = VOICE_SAMPLE_RATE * 2 * 20
FFMPEG_TIMEOUT: Final = 20.0
ATVV_CODEC_ADPCM_16K: Final = 0x02
ATVV_MAX_PACKET_BYTES: Final = 512

_IMA_INDEX_TABLE: Final = (
    -1,
    -1,
    -1,
    -1,
    2,
    4,
    6,
    8,
    -1,
    -1,
    -1,
    -1,
    2,
    4,
    6,
    8,
)
_IMA_STEP_TABLE: Final = (
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    19,
    21,
    23,
    25,
    28,
    31,
    34,
    37,
    41,
    45,
    50,
    55,
    60,
    66,
    73,
    80,
    88,
    97,
    107,
    118,
    130,
    143,
    157,
    173,
    190,
    209,
    230,
    253,
    279,
    307,
    337,
    371,
    408,
    449,
    494,
    544,
    598,
    658,
    724,
    796,
    876,
    963,
    1060,
    1166,
    1282,
    1411,
    1552,
    1707,
    1878,
    2066,
    2272,
    2499,
    2749,
    3024,
    3327,
    3660,
    4026,
    4428,
    4871,
    5358,
    5894,
    6484,
    7132,
    7845,
    8630,
    9493,
    10442,
    11487,
    12635,
    13899,
    15289,
    16818,
    18500,
    20350,
    22385,
    24623,
    27086,
    29794,
    32767,
)


class VoiceTransportError(RuntimeError):
    """Raised when a remote voice stream cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class HidVoicePacket:
    """One validated Opus packet received through a HID input report."""

    report_id: int
    characteristic_handle: int
    data: bytes


@dataclass(frozen=True, slots=True)
class PcmVoicePacket:
    """One decoded mono PCM chunk from a proprietary remote transport."""

    sample_rate: int
    data: bytes


type VoicePacket = HidVoicePacket | PcmVoicePacket


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


@dataclass(slots=True)
class PcmVoiceBuffer:
    """Collect one bounded 16 kHz mono signed-16 PCM utterance."""

    max_bytes: int = MAX_PCM_BYTES
    chunks: list[bytes] = field(default_factory=list)
    byte_count: int = 0

    def append(self, packet: PcmVoicePacket) -> None:
        """Append one validated PCM chunk without allowing unbounded growth."""
        if packet.sample_rate != VOICE_SAMPLE_RATE:
            raise VoiceTransportError("unsupported PCM sample rate")
        if not packet.data or len(packet.data) % 2:
            raise VoiceTransportError("invalid signed-16 PCM chunk")
        if self.byte_count + len(packet.data) > self.max_bytes:
            raise VoiceTransportError("PCM voice recording exceeded its size limit")
        self.chunks.append(packet.data)
        self.byte_count += len(packet.data)

    def to_pcm(self) -> bytes:
        """Return one bounded Assist-compatible PCM stream."""
        if not self.chunks:
            raise VoiceTransportError("PCM voice recording is empty")
        return b"".join(self.chunks)


@dataclass(slots=True)
class AtvvImaAdpcmDecoder:
    """Decode Android TV Voice v1.0's continuous IMA ADPCM stream."""

    predictor: int = 0
    step_index: int = 1

    def reset(self, predictor: int = 0, step_index: int = 1) -> None:
        """Apply the default state or an ATVV synchronization point."""
        if not -32768 <= predictor <= 32767:
            raise VoiceTransportError("ATVV ADPCM predictor is out of range")
        if not 0 <= step_index < len(_IMA_STEP_TABLE):
            raise VoiceTransportError("ATVV ADPCM step index is out of range")
        self.predictor = predictor
        self.step_index = step_index

    def decode(self, data: bytes | bytearray) -> bytes:
        """Decode one bounded chunk, preserving state across notifications."""
        value = bytes(data)
        if not value or len(value) > ATVV_MAX_PACKET_BYTES:
            raise VoiceTransportError("invalid ATVV ADPCM packet size")

        output = bytearray(len(value) * 4)
        offset = 0
        for packed_codes in value:
            # Android TV remote firmware sends the first sample in the high
            # nibble, unlike the low-nibble-first WAV IMA block layout.
            for code in (packed_codes >> 4, packed_codes & 0x0F):
                step = _IMA_STEP_TABLE[self.step_index]
                difference = step >> 3
                if code & 0x04:
                    difference += step
                if code & 0x02:
                    difference += step >> 1
                if code & 0x01:
                    difference += step >> 2
                self.predictor += -difference if code & 0x08 else difference
                self.predictor = min(32767, max(-32768, self.predictor))
                self.step_index += _IMA_INDEX_TABLE[code]
                self.step_index = min(88, max(0, self.step_index))
                pack_into("<h", output, offset, self.predictor)
                offset += 2
        return bytes(output)


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
