"""Twilio WebSocket handler — parses handshake, runs pipeline, persists CallResult."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from loguru import logger
from starlette.websockets import WebSocket

from pipecat.pipeline.task import PipelineParams
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from core.call_lifecycle import run_call, safe_save_call, supabase_configured
from core.call_result import CallResult
from core.call_store import increment_batch_counter
from core.config_loader import load_agent_config
from core.pipeline import build_pipeline, build_pipeline_components


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

    agent_name = body.get("agent_name")
    if not agent_name:
        agent_name = "chris/claim_status"
        logger.warning(
            f"No agent_name in WebSocket handshake for call {call_sid}, "
            f"defaulting to {agent_name}"
        )
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
        agent_display_name=config.display_name,
        target_number=target_number,
        direction=direction,
        status="in_progress",
        started_at=datetime.now(timezone.utc),
        case_data=case_data,
        batch_id=batch_id,
        batch_row_index=batch_row_index,
    )
    await safe_save_call(result)

    await run_call(bundle, transport, config, result, case_data)

    if result.batch_id and supabase_configured():
        try:
            if result.status == "completed":
                await increment_batch_counter(result.batch_id, "completed_rows")
            else:
                await increment_batch_counter(result.batch_id, "failed_rows")
        except Exception:
            logger.exception("Failed to update batch counters")
