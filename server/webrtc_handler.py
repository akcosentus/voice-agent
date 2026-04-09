"""WebRTC test call handler — same pipeline as Twilio, different transport."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from loguru import logger

from pipecat.pipeline.task import PipelineParams
from pipecat.processors.frameworks.rtvi import RTVIObserverParams
from pipecat.processors.frameworks.rtvi.observer import RTVIFunctionCallReportLevel
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

from core.call_lifecycle import run_call, safe_save_call
from core.call_result import CallResult
from core.config_loader import load_agent_config, load_agent_draft
from core.pipeline import build_pipeline, build_pipeline_components


async def run_test_call(
    connection: SmallWebRTCConnection,
    agent_name: str,
    use_draft: bool = False,
    case_data: dict | None = None,
):
    """Run a test call over WebRTC — full pipeline identical to phone calls."""
    case_data = case_data or {}
    call_key = str(uuid.uuid4())
    logger.info(
        f"[TEST CALL] Starting agent={agent_name} draft={use_draft} "
        f"call_id={call_key} case_keys={list(case_data.keys())}"
    )

    if use_draft:
        config = load_agent_draft(agent_name)
    else:
        config = load_agent_config(agent_name)

    components = build_pipeline_components(config, case_data, transport_type="livekit")

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
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
        target_number="webrtc",
        direction="test",
        status="in_progress",
        started_at=datetime.now(timezone.utc),
        case_data=case_data,
    )
    await safe_save_call(result)

    await run_call(bundle, transport, config, result, case_data)

    logger.info(
        f"[TEST CALL] Ended — {config.display_name} "
        f"duration={result.duration_secs:.1f}s "
        f"transcript_turns={len(result.transcript)} "
        f"status={result.status}"
    )
