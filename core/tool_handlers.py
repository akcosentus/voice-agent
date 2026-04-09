"""Central registry of tool handler functions for voice agents.

Tool *schemas* (what the LLM sees) live in pipeline.py.  This module holds the
Python handlers that execute when the LLM invokes a tool.

Every handler receives (params, task, settings) where settings is the tool's
settings dict from the agent config (e.g. transfer targets).
"""

import os

from loguru import logger
from pipecat.frames.frames import EndFrame
from pipecat.pipeline.task import PipelineTask
from pipecat.services.llm_service import FunctionCallParams


async def end_call(params: FunctionCallParams, task: PipelineTask, settings: dict):
    """End the call gracefully after the LLM's goodbye finishes playing."""
    await params.result_callback({"status": "call_ended"})
    await task.queue_frame(EndFrame())


async def transfer_call(params: FunctionCallParams, task: PipelineTask, settings: dict):
    """Transfer the call to a target by looking up the phone number from settings.

    Requires a real Twilio call (call_sid on the task). WebRTC test calls
    get a simulated success so the agent can demonstrate the decision.
    """
    target_name = params.arguments.get("target", "")
    targets = settings.get("targets", {})
    phone_number = targets.get(target_name)

    if not phone_number:
        logger.warning(f"Transfer target '{target_name}' not found in settings: {targets}")
        await params.result_callback({"status": "error", "message": f"Unknown target: {target_name}"})
        return

    call_sid = getattr(task, "_call_sid", None)
    if not call_sid:
        logger.info(f"Transfer simulated (no Twilio SID): {target_name} → {phone_number}")
        await params.result_callback({
            "status": "simulated",
            "message": f"In a live call, this would transfer to {target_name} ({phone_number})",
        })
        return

    try:
        from twilio.rest import Client as TwilioClient

        client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN"),
        )
        client.calls(call_sid).update(
            twiml=f'<Response><Dial>{phone_number}</Dial></Response>'
        )
        logger.info(f"Transferred call {call_sid} to {target_name} ({phone_number})")
        await params.result_callback({"status": "transferred", "target": target_name})
    except Exception as e:
        logger.exception(f"Transfer to {target_name} failed")
        await params.result_callback({"status": "error", "message": str(e)})


TOOL_HANDLERS: dict[str, callable] = {
    "end_call": end_call,
    "transfer_call": transfer_call,
}
