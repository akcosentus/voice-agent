"""Local patches/overrides of Pipecat services.

We prefer to subclass rather than monkey-patch so upgrades remain safe.
Each override documents the exact upstream method it overrides, the Pipecat
version we verified against, and why the override exists.
"""

from __future__ import annotations

import ormsgpack
from loguru import logger
from pipecat.frames.frames import TTSAudioRawFrame
from pipecat.services.fish.tts import FishAudioTTSService


# Minimum audio chunk size (in bytes) to forward downstream from Fish's
# WebSocket stream. Upstream Pipecat uses 1024 to skip msgpack framing
# artifacts, but 1024 bytes @ 8kHz mono 16-bit PCM = ~64ms of real speech,
# and Fish flushes on punctuation. That means short spoken fragments like
# "K as in Kilo --" on Twilio 8kHz calls can be silently discarded,
# producing the "agent says 'Sure.' then silence during NATO spelling"
# bug we reproduced on 2026-04-17 18:48.
#
# Reference: /usr/local/lib/python3.11/site-packages/pipecat/services/fish/tts.py
# (Pipecat 0.0.108, line 378). Raise upstream as a configurable threshold.
_MIN_AUDIO_CHUNK_BYTES = 64


class FishAudioTTSServicePatched(FishAudioTTSService):
    """FishAudioTTSService with a lower minimum-chunk filter.

    Override of `FishAudioTTSService._receive_messages` (Pipecat 0.0.108).
    The parent implementation filters out audio chunks smaller than 1024
    bytes; on 8kHz phone audio that loses real speech content around
    punctuation-driven Fish flushes. This subclass lowers the threshold
    to 64 bytes — still enough to drop msgpack framing noise, but small
    enough that short spoken fragments (NATO phonetic syllables,
    commas in natural speech) survive.

    If a future Pipecat upgrade restructures this method, verify the
    upstream implementation and update this override. Everything else on
    the class (connect, disconnect, run_tts, start/stop, settings) is
    unchanged and inherited from the parent.
    """

    async def _receive_messages(self):
        async for message in self._get_websocket():
            try:
                if not isinstance(message, bytes):
                    continue
                msg = ormsgpack.unpackb(message)
                if not isinstance(msg, dict):
                    continue

                event = msg.get("event")
                if event == "audio":
                    audio_data = msg.get("audio")
                    # DEBUG: log every chunk size so we can confirm whether
                    # the 1024-byte filter was the issue or whether Fish is
                    # emitting tiny chunks / no chunks at all.
                    size = len(audio_data) if audio_data else 0
                    kept = size > _MIN_AUDIO_CHUNK_BYTES
                    logger.debug(
                        f"[Fish chunk] size={size}B kept={kept} "
                        f"(threshold={_MIN_AUDIO_CHUNK_BYTES}B)"
                    )
                    if audio_data and kept:
                        context_id = self.get_active_audio_context_id()
                        frame = TTSAudioRawFrame(
                            audio_data,
                            self.sample_rate,
                            1,
                            context_id=context_id,
                        )
                        await self.append_to_audio_context(context_id, frame)
                        await self.stop_ttfb_metrics()
                elif event == "finish":
                    reason = msg.get("reason", "unknown")
                    if reason == "error":
                        await self.push_error(
                            error_msg="Fish Audio server error during synthesis"
                        )
                    else:
                        logger.debug(f"Fish Audio session finished: {reason}")
            except Exception as e:
                await self.push_error(
                    error_msg=f"Unknown error occurred: {e}", exception=e
                )
