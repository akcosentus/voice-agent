"""LiveKit transport handler — runs a voice agent as a LiveKit bot.

Parallel to :mod:`server.ws_handler` (Twilio Media Streams) and
:mod:`server.webrtc_handler` (SmallWebRTC browser test). Handler is
intentionally transport-specific: LiveKit's event surface is participant-
centric, not client-centric, and abstracting the three transports behind
a unified event API is premature until we've seen all three shapes side
by side (see ``docs/livekit_local_dev.md`` for rationale).

This handler owns the full call lifecycle (recording, transcript
capture, post-call analysis, persistence) in the same shape as
:func:`core.call_lifecycle.run_call`, but with LiveKit-native event
names. The duplication is deliberate per PR direction; extract later if
it proves stable across transports.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
import wave
from datetime import datetime, timezone

from loguru import logger

from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams
from pipecat.processors.frameworks.rtvi import RTVIObserverParams
from pipecat.processors.frameworks.rtvi.observer import RTVIFunctionCallReportLevel
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport

from core.call_lifecycle import safe_save_call
from core.call_result import CallResult
from core.config_loader import load_agent_config
from core.pipeline import build_pipeline, build_pipeline_components
from core.post_call import run_post_call_analyses

S3_BUCKET = os.getenv("S3_BUCKET", "medcloud-voice-us-prod-825")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")


async def _upload_recording_s3(
    call_key: str,
    audio: bytes,
    sample_rate: int,
    num_channels: int,
) -> str | None:
    """Encode raw audio as WAV and upload to S3. Returns the S3 key or None.

    Mirrors :func:`core.call_lifecycle._upload_recording_s3`. Kept local so
    the LiveKit handler is fully self-contained against a future refactor
    of the Twilio lifecycle; extract to a shared module if a third caller
    appears.
    """
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


async def run_livekit_agent(
    url: str,
    token: str,
    room_name: str,
    agent_name: str,
    case_data: dict | None = None,
) -> None:
    """Run a voice agent as a LiveKit bot. Returns when the last
    participant leaves the room.

    The caller provides a bot token; human participants join the room
    separately with their own tokens (browser, SIP gateway, etc.).

    Args:
        url: LiveKit server URL. ``ws://`` for local dev,
            ``wss://`` in production.
        token: JWT authorising the bot to join ``room_name`` with
            publish + subscribe grants.
        room_name: LiveKit room to join.
        agent_name: Agent config key in Aurora (e.g. ``chris-claim-status``).
        case_data: Hydration variables for the agent's system prompt.
    """
    case_data = case_data or {}
    call_key = str(uuid.uuid4())

    config = load_agent_config(agent_name)
    logger.info(
        f"[LiveKit] Starting agent={agent_name} ({config.display_name}) "
        f"room={room_name} call_id={call_key} "
        f"case_keys={sorted(case_data.keys())}"
    )

    components = build_pipeline_components(
        config, case_data, transport_type="livekit"
    )

    transport = LiveKitTransport(
        url=url,
        token=token,
        room_name=room_name,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            camera_in_enabled=False,
            camera_out_enabled=False,
            video_in_enabled=False,
            video_out_enabled=False,
            # Sample rates intentionally at pipecat defaults (16kHz in for
            # Smart Turn v3, 24kHz out for Fish native). No μ-law leg to
            # resample for, unlike Twilio.
        ),
    )

    bundle = build_pipeline(
        components,
        transport,
        config=config,
        pipeline_params=PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
        ),
        call_id=call_key,
        rtvi_observer_params=RTVIObserverParams(
            function_call_report_level={"*": RTVIFunctionCallReportLevel.FULL},
        ),
    )

    result = CallResult(
        call_id=call_key,
        agent_name=agent_name,
        agent_display_name=config.display_name,
        target_number=f"livekit:{room_name}",
        direction="livekit",
        status="in_progress",
        started_at=datetime.now(timezone.utc),
        case_data=case_data,
    )
    await safe_save_call(result)

    recording_uploaded = False

    if bundle.audiobuffer:

        @bundle.audiobuffer.event_handler("on_audio_data")
        async def on_audio_data(_proc, audio, sample_rate, num_channels):
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

    @transport.event_handler("on_connected")
    async def on_bot_connected(_transport):
        logger.info(
            f"[LiveKit] Bot joined room={room_name} — awaiting participant"
        )

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(_transport, participant_id):
        logger.info(
            f"[LiveKit] First participant joined id={participant_id} — "
            f"{config.display_name} is listening"
        )
        if bundle.audiobuffer:
            await bundle.audiobuffer.start_recording()

    @transport.event_handler("on_participant_left")
    async def on_participant_left(_transport, participant_id, reason):
        logger.info(
            f"[LiveKit] Participant left id={participant_id} reason={reason} "
            "— cancelling pipeline task"
        )
        await bundle.task.cancel()

    runner = PipelineRunner(handle_sigint=False)

    try:
        await runner.run(bundle.task)
    except Exception as e:
        logger.exception("LiveKit pipeline run failed")
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

        logger.info(
            f"[LiveKit] Ended — {config.display_name} "
            f"duration={result.duration_secs:.1f}s "
            f"transcript_turns={len(result.transcript)} "
            f"status={result.status}"
        )
