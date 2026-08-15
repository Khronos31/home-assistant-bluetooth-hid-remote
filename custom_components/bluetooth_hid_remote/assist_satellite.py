"""Push-to-talk Assist satellite for voice-capable Bluetooth HID remotes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, override

from homeassistant.components import assist_satellite
from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.assist_satellite.entity import AssistSatelliteState
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.components.media_player.const import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.components.media_player.const import (
    DOMAIN as MEDIA_PLAYER_DOMAIN,
)
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BluetoothHidRemoteConfigEntry
from .const import CONF_VOICE_RESPONSE_PLAYER, DOMAIN, EVENT_KEY_RELEASED
from .manager import HidInputReport
from .voice import (
    MAX_VOICE_PACKETS,
    HidVoicePacket,
    OpusVoiceBuffer,
    PcmVoiceBuffer,
    PcmVoicePacket,
    VoicePacket,
    VoiceTransportError,
    async_decode_opus_packets,
)

_LOGGER = logging.getLogger(__name__)

VOICE_TIMEOUT_SECONDS = 15.0
SEARCH_USAGE = (0x0C, 0x0221)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BluetoothHidRemoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a satellite only when the HID descriptor supports voice."""
    manager = entry.runtime_data
    if manager.supports_voice:
        async_add_entities([BluetoothHidRemoteAssistSatellite(entry)])


class BluetoothHidRemoteAssistSatellite(assist_satellite.AssistSatelliteEntity):
    """Turn a remote's search key and Opus reports into one Assist run."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:microphone"
    entity_description = assist_satellite.AssistSatelliteEntityDescription(
        key="assist", translation_key="assist"
    )

    def __init__(self, entry: BluetoothHidRemoteConfigEntry) -> None:
        self._entry = entry
        self._manager = entry.runtime_data
        compact_address = self._manager.address.replace(":", "").lower()
        self._compact_address = compact_address
        self._attr_unique_id = f"{compact_address}_assist_satellite"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._manager.address)},
            connections={(CONNECTION_BLUETOOTH, self._manager.address)},
            manufacturer="Bluetooth SIG",
            model="BLE HID Remote",
            name=self._manager.name,
        )
        self._voice_buffer: OpusVoiceBuffer | PcmVoiceBuffer | None = None
        self._voice_timeout: asyncio.TimerHandle | None = None
        self._processing_task: asyncio.Task[Any] | None = None

    @property
    @override
    def pipeline_entity_id(self) -> str | None:
        """Return this remote's Assist pipeline selector entity ID."""
        return er.async_get(self.hass).async_get_entity_id(
            Platform.SELECT, DOMAIN, f"{self._compact_address}-pipeline"
        )

    @callback
    @override
    def async_get_configuration(
        self,
    ) -> assist_satellite.AssistSatelliteConfiguration:
        """Return a push-to-talk-only satellite configuration."""
        return assist_satellite.AssistSatelliteConfiguration(
            available_wake_words=[], active_wake_words=[], max_active_wake_words=0
        )

    @override
    async def async_set_configuration(
        self, config: assist_satellite.AssistSatelliteConfiguration
    ) -> None:
        """Reject wake words because this transport is push-to-talk only."""
        if config.active_wake_words:
            raise HomeAssistantError(
                "Bluetooth HID Remote supports push-to-talk, not wake words"
            )

    async def async_added_to_hass(self) -> None:
        """Subscribe to key controls and the dedicated voice transport."""
        await super().async_added_to_hass()
        self.async_on_remove(self._manager.async_add_listener(self._receive_report))
        self.async_on_remove(
            self._manager.async_add_voice_listener(self._receive_voice_packet)
        )
        self.async_on_remove(self._cancel_local_tasks)

    @callback
    def _receive_report(self, report: HidInputReport) -> None:
        """Use the Consumer AC Search key as the push-to-talk control."""
        if not any(
            (usage.usage_page, usage.usage_id) == SEARCH_USAGE
            for usage in report.usages
        ):
            return
        if report.event_type == EVENT_KEY_RELEASED:
            self._finish_recording()
        else:
            self._start_recording()

    @callback
    def _receive_voice_packet(self, packet: VoicePacket | None) -> None:
        """Collect one validated voice transport, or finish on stop."""
        if packet is None:
            self._finish_recording()
            return
        expected_type = (
            PcmVoiceBuffer if isinstance(packet, PcmVoicePacket) else OpusVoiceBuffer
        )
        if self._voice_buffer is None:
            self._start_recording(expected_type())
        if self._voice_buffer is None:
            # A late packet from a just-finished utterance must not start a
            # second pipeline while the first one is decoding.
            return
        if not isinstance(self._voice_buffer, expected_type):
            _LOGGER.warning("Stopped mixed voice transports for %s", self.name)
            self._finish_recording()
            return
        try:
            if isinstance(packet, HidVoicePacket):
                self._voice_buffer.append(packet.data)
            else:
                self._voice_buffer.append(packet)
        except VoiceTransportError as err:
            _LOGGER.warning("Stopped HID voice capture for %s: %s", self.name, err)
            self._finish_recording()

    @callback
    def _start_recording(
        self, buffer: OpusVoiceBuffer | PcmVoiceBuffer | None = None
    ) -> None:
        """Start one bounded recording and ignore overlapping pipeline work."""
        if self._voice_buffer is not None:
            return
        if self._processing_task is not None and not self._processing_task.done():
            _LOGGER.debug("Ignored HID voice press while Assist is still processing")
            return
        self._voice_buffer = buffer or OpusVoiceBuffer(max_packets=MAX_VOICE_PACKETS)
        self._set_state(AssistSatelliteState.LISTENING)
        self._voice_timeout = self.hass.loop.call_later(
            VOICE_TIMEOUT_SECONDS, self._finish_recording
        )

    @callback
    def _finish_recording(self) -> None:
        """Close capture and schedule exactly one decode/pipeline run."""
        if self._voice_timeout is not None:
            self._voice_timeout.cancel()
            self._voice_timeout = None
        buffer = self._voice_buffer
        self._voice_buffer = None
        if buffer is None or not (
            buffer.packets if isinstance(buffer, OpusVoiceBuffer) else buffer.chunks
        ):
            self._set_state(AssistSatelliteState.IDLE)
            return
        self._processing_task = self._entry.async_create_background_task(
            self.hass,
            self._async_process_recording(buffer),
            name=f"Bluetooth HID Assist {self._manager.address}",
        )

    async def _async_process_recording(
        self, buffer: OpusVoiceBuffer | PcmVoiceBuffer
    ) -> None:
        """Decode one utterance, run STT/intent, and optionally play TTS."""
        try:
            if isinstance(buffer, OpusVoiceBuffer):
                pcm = await async_decode_opus_packets(
                    get_ffmpeg_manager(self.hass).binary, list(buffer.packets)
                )
            else:
                pcm = buffer.to_pcm()

            async def audio_stream() -> AsyncIterator[bytes]:
                yield pcm

            response_player = self._entry.options.get(CONF_VOICE_RESPONSE_PLAYER)
            await self.async_accept_pipeline_from_satellite(
                audio_stream(),
                start_stage=PipelineStage.STT,
                end_stage=(
                    PipelineStage.TTS if response_player else PipelineStage.INTENT
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Bluetooth HID Assist run failed for %s", self.name)
            self._set_state(AssistSatelliteState.IDLE)
        finally:
            self._processing_task = None

    @callback
    @override
    def on_pipeline_event(self, event: PipelineEvent) -> None:
        """Play a generated response on the configured media player."""
        if event.type is not PipelineEventType.TTS_END or not event.data:
            return
        response_player = self._entry.options.get(CONF_VOICE_RESPONSE_PLAYER)
        output = event.data.get("tts_output")
        if not response_player or not isinstance(output, dict):
            self.tts_response_finished()
            return
        self._entry.async_create_background_task(
            self.hass,
            self._async_play_response(response_player, output),
            name=f"Bluetooth HID Assist response {self._manager.address}",
        )

    async def _async_play_response(
        self, response_player: str, output: dict[str, Any]
    ) -> None:
        """Send the pipeline TTS URL to one explicitly selected player."""
        try:
            media_id = output.get("url") or output.get("media_id")
            if not isinstance(media_id, str):
                raise VoiceTransportError("Assist returned no playable TTS media")
            await self.hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                SERVICE_PLAY_MEDIA,
                {
                    ATTR_ENTITY_ID: response_player,
                    ATTR_MEDIA_CONTENT_ID: async_process_play_media_url(
                        self.hass, media_id
                    ),
                    ATTR_MEDIA_CONTENT_TYPE: output.get("mime_type", "audio/mpeg"),
                },
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("Could not play Bluetooth HID Assist response")
        finally:
            self.tts_response_finished()

    @callback
    def _cancel_local_tasks(self) -> None:
        """Cancel capture/decode work when the entity unloads."""
        if self._voice_timeout is not None:
            self._voice_timeout.cancel()
            self._voice_timeout = None
        self._voice_buffer = None
        if self._processing_task is not None:
            self._processing_task.cancel()
            self._processing_task = None

    @override
    async def async_announce(
        self, announcement: assist_satellite.AssistSatelliteAnnouncement
    ) -> None:
        """Reject announcements because the remote has no speaker."""
        raise HomeAssistantError("Bluetooth HID remotes cannot play announcements")
