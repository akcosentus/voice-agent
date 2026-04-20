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

S3_BUCKET = os.getenv("S3_BUCKET", "medcloud-voice-us-prod-825")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")


async def _upload_recording_s3(
    call_key: str,
    audio: bytes,
    sample_rate: int,
    num_channels: int,
) -> str | None:
    """Encode raw audio as WAV and upload to S3. Returns the S3 key or None."""
    if not audio:
        return None
    try:
        import boto3

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(num_channels)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(audio)
        data = buf.getvalue()

        key = f"recordings/{call_key}.wav"
        s3 = boto3.client("s3", region_name=AWS_REGION)
        await asyncio.to_thread(
            s3.put_object,
            Bucket=S3_BUCKET,
            Key=key,
            Body=data,
            ContentType="audio/wav",
        )
        return key
    except Exception:
        logger.exception("Failed to upload recording to S3")
        return None


async def _safe_save_call(result: CallResult, *, is_final: bool = False) -> None:
    """Persist call record with retry on final save."""
    try:
        await save_call_result(result)
    except Exception as e:
        if is_final:
            logger.error(
                "FINAL save failed for call %s, retrying once: %s",
                result.call_id, e,
            )
            try:
                await asyncio.sleep(1)
                await save_call_result(result)
            except Exception as e2:
                logger.critical(
                    "FINAL save LOST for call %s: %s", result.call_id, e2,
                )
        else:
            logger.exception("Failed to save call result")


# Re-export for callers that import from here
safe_save_call = _safe_save_call


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
    analysis, and persists everything to the Lambda data layer.

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
            # Recording is best-effort — never let upload failures kill the call.
            nonlocal recording_uploaded
            if recording_uploaded or not audio:
                return
            try:
                path = await _upload_recording_s3(
                    result.call_id, audio, sample_rate, num_channels
                )
            except Exception:
                logger.exception("Recording handler crashed — continuing call")
                path = None
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
        await _safe_save_call(result, is_final=True)

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
            await _safe_save_call(result, is_final=True)

        # Tell the queue consumer the batch row is terminal so it can release
        # its concurrency slot and (if all rows done) finalize the batch.
        if result.batch_row_id:
            try:
                from core.lambda_client import invoke as _lambda_invoke
                await _lambda_invoke(
                    "PUT",
                    f"/batch-rows/{result.batch_row_id}",
                    body={"status": result.status, "call_id": result.call_id},
                )
            except Exception:
                logger.exception(
                    "Failed to write terminal status to batch row %s",
                    result.batch_row_id,
                )
