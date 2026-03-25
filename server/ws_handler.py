"""Twilio WebSocket handler — parses handshake, runs pipeline, persists CallResult."""

from __future__ import annotations

import io
import os
import wave
from datetime import datetime, timezone

from loguru import logger
from starlette.websockets import WebSocket

from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from core.call_result import CallResult
from core.call_store import increment_batch_counter, save_call_result
from core.config_loader import load_agent_config
from core.pipeline import PipelineBundle, build_pipeline, build_pipeline_components
from core.post_call import run_post_call_analyses


def _supabase_configured() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY"))


async def _safe_save_call(result: CallResult) -> None:
    if not _supabase_configured():
        logger.warning("Supabase not configured; skipping call persistence")
        return
    try:
        await save_call_result(result)
    except Exception:
        logger.exception("Failed to save call result to Supabase")


async def _upload_recording_wav(
    call_key: str,
    audio: bytes,
    sample_rate: int,
    num_channels: int,
) -> str | None:
    if not _supabase_configured() or not audio:
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


async def handle_twilio_websocket(
    websocket: WebSocket,
    pending_calls: dict[str, dict],
):
    """Handle a Twilio Media Stream WebSocket connection."""
    transport_type, call_data = await parse_telephony_websocket(websocket)
    logger.info(f"Detected transport: {transport_type}, call_data: {call_data}")

    if transport_type != "twilio":
        logger.error(f"Expected Twilio transport, got: {transport_type}")
        return

    stream_sid = call_data.get("stream_id", "")
    call_sid = call_data.get("call_id", "")
    body = call_data.get("body", {})

    agent_name = body.get("agent_name", "chris/claim_status")
    pending_key = body.get("call_id")

    logger.info(f"Stream SID: {stream_sid}, Call SID: {call_sid}, Agent: {agent_name}")

    pending = None
    if pending_key and pending_key in pending_calls:
        pending = pending_calls.pop(pending_key)
        logger.info(f"Loaded pending payload for call_id={pending_key}")
    else:
        logger.info("No pending case data — inbound call or missing call_id")

    case_data = pending.get("case_data", {}) if pending else {}
    target_number = (pending.get("target_number") if pending else None) or body.get(
        "From", ""
    ) or "inbound"
    batch_id = pending.get("batch_id") if pending else None
    batch_row_index = pending.get("batch_row_index") if pending else None
    direction = "outbound" if pending else "inbound"

    call_key = pending_key or call_sid

    config = load_agent_config(agent_name)
    components = build_pipeline_components(config, case_data, transport_type="twilio")

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    bundle = build_pipeline(
        components,
        transport,
        config=config,
        pipeline_params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
        ),
        call_id=call_key,
    )

    result = CallResult(
        call_id=call_key,
        agent_name=agent_name,
        target_number=target_number,
        direction=direction,
        status="in_progress",
        started_at=datetime.now(timezone.utc),
        case_data=case_data,
        batch_id=batch_id,
        batch_row_index=batch_row_index,
    )
    await _safe_save_call(result)

    recording_uploaded = False

    if bundle.audiobuffer:

        @bundle.audiobuffer.event_handler("on_audio_data")
        async def on_audio_data(_proc, audio, sample_rate, num_channels):
            nonlocal recording_uploaded
            if recording_uploaded or not audio:
                return
            path = await _upload_recording_wav(call_key, audio, sample_rate, num_channels)
            if path:
                result.recording_path = path
                recording_uploaded = True

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
        await _safe_save_call(result)

        if result.status == "completed" and config.post_call_analyses:
            try:
                result.post_call_analyses = await run_post_call_analyses(
                    config, result.transcript, case_data
                )
            except Exception:
                logger.exception("Post-call analyses failed")
            await _safe_save_call(result)

        if result.batch_id and _supabase_configured():
            try:
                if result.status == "completed":
                    await increment_batch_counter(result.batch_id, "completed_rows")
                else:
                    await increment_batch_counter(result.batch_id, "failed_rows")
            except Exception:
                logger.exception("Failed to update batch counters")
