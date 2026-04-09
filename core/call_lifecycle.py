"""Shared call lifecycle — recording, pipeline run, post-call analysis, persistence.

Both Twilio (ws_handler) and WebRTC (webrtc_handler) call ``run_call()``
so every call goes through identical processing regardless of transport.
"""

from __future__ import annotations

import asyncio
import io
import os
import wave
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from pipecat.pipeline.runner import PipelineRunner

from core.call_result import CallResult
from core.call_store import save_call_result
from core.config_loader import AgentConfig
from core.pipeline import PipelineBundle
from core.post_call import run_post_call_analyses


def supabase_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"))


async def safe_save_call(result: CallResult, *, is_final: bool = False) -> None:
    if not supabase_configured():
        logger.warning("Supabase not configured; skipping call persistence")
        return
    try:
        await save_call_result(result)
    except Exception as e:
        if is_final:
            logger.error(
                f"FINAL save failed for call {result.call_id}, retrying once: {e}"
            )
            try:
                await asyncio.sleep(1)
                await save_call_result(result)
            except Exception as e2:
                logger.critical(
                    f"FINAL save LOST for call {result.call_id}: {e2}"
                )
        else:
            logger.exception("Failed to save call result to Supabase")


async def upload_recording_wav(
    call_key: str,
    audio: bytes,
    sample_rate: int,
    num_channels: int,
) -> str | None:
    if not supabase_configured() or not audio:
        return None
    try:
        from core.supabase_client import get_supabase

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(num_channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio)
        data = buf.getvalue()
        path = f"recordings/{call_key}.wav"
        get_supabase().storage.from_("recordings").upload(
            path,
            data,
            file_options={"content-type": "audio/wav", "upsert": "true"},
        )
        return path
    except Exception:
        logger.exception("Failed to upload recording to Supabase Storage")
        return None


async def run_call(
    bundle: PipelineBundle,
    transport,
    config: AgentConfig,
    result: CallResult,
    case_data: dict[str, Any],
) -> None:
    """Execute a call through the full lifecycle.

    This is the single code path for ALL calls (Twilio, WebRTC, any future
    transport).  It wires recording capture, transport events, runs the
    pipeline, captures the transcript, uploads the recording, runs post-call
    analysis, and persists everything to Supabase.

    The caller is responsible for:
      - building the transport
      - calling ``build_pipeline_components`` + ``build_pipeline``
      - creating the initial ``CallResult`` and saving it
      - any transport-specific post-processing (e.g. batch counters)
    """
    recording_uploaded = False

    if bundle.audiobuffer:

        @bundle.audiobuffer.event_handler("on_audio_data")
        async def on_audio_data(_proc, audio, sample_rate, num_channels):
            nonlocal recording_uploaded
            if recording_uploaded or not audio:
                return
            path = await upload_recording_wav(
                result.call_id, audio, sample_rate, num_channels
            )
            if path:
                result.recording_path = path
                recording_uploaded = True
            else:
                prev = result.error or ""
                result.error = (
                    f"{prev} | Recording upload failed" if prev else
                    "Recording upload failed"
                )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"{config.display_name} connected — call in progress")
        if bundle.audiobuffer:
            await bundle.audiobuffer.start_recording()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"{config.display_name} — client disconnected")
        await bundle.task.cancel()

    runner = PipelineRunner(handle_sigint=False)

    try:
        await runner.run(bundle.task)
    except Exception as e:
        logger.exception("Pipeline run failed")
        result.status = "failed"
        result.error = str(e)
    else:
        if result.status == "in_progress":
            result.status = "completed"
    finally:
        result.ended_at = datetime.now(timezone.utc)
        if result.started_at and result.ended_at:
            result.duration_secs = (
                result.ended_at - result.started_at
            ).total_seconds()
        result.transcript = list(bundle.transcript_log)
        await safe_save_call(result, is_final=True)

        if result.status == "completed" and config.post_call_analyses:
            try:
                result.post_call_analyses = await run_post_call_analyses(
                    config, result.transcript, case_data
                )
            except Exception as e:
                logger.exception("Post-call analyses failed")
                result.post_call_analyses = {
                    "_error": f"Analysis failed: {str(e)[:200]}"
                }
            await safe_save_call(result, is_final=True)
