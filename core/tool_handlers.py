"""Central registry of tool handler functions for voice agents.

Tool *schemas* (what the LLM sees) live in pipeline.py.  This module holds the
Python handlers that execute when the LLM invokes a tool.

Every handler receives (params, task, settings) where settings is the tool's
settings dict from the agent config (e.g. transfer targets).

Note on DTMF:
  Two paths exist:
    - press_digit tool (this file) — used when an agent has NO ivr_goal.
      Claude calls the tool, we push InterruptionTaskFrame to cut any
      in-flight TTS filler, then queue OutputDTMFUrgentFrame per digit.
      Result is returned with run_llm=False so the bot stays silent after
      pressing (the IVR's next prompt becomes the next user turn).
    - IVRNavigator XML tags (pipecat.extensions.ivr) — used when an agent
      HAS ivr_goal. Claude emits <dtmf>N</dtmf> inline in LLM text;
      IVRProcessor strips the tags from the TTS stream and dispatches
      DTMF directly. No tool call involved.
  Agents pick one or the other based on ivr_goal. press_digit stays in
  the TOOL_HANDLERS registry so conversation-first agents with occasional
  IVR needs (e.g. mid-call transfer through a menu) can use it.
"""

import os

from loguru import logger
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.frames.frames import (
    EndFrame,
    InterruptionTaskFrame,
    OutputDTMFUrgentFrame,
)
from pipecat.pipeline.task import PipelineTask
from pipecat.services.llm_service import (
    FunctionCallParams,
    FunctionCallResultProperties,
)

_VALID_DTMF = frozenset("0123456789*#")


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


async def press_digit(params: FunctionCallParams, task: PipelineTask, settings: dict):
    """Send DTMF tones through the pipeline transport.

    Uses Pipecat's OutputDTMFUrgentFrame which the TwilioFrameSerializer
    handles natively. On WebRTC test calls the frame is harmless (no phone
    line to receive tones).

    ── Silence-while-pressing behavior ──
    Claude frequently emits conversational text alongside tool_use blocks
    (e.g. "sure" or "Yeah, it's 494-472-929" + press_digit). On real Twilio
    calls the DTMF is out-of-band so the far end hears only the spoken
    text, which is what the rep asked for. On WebRTC demos the DTMF leaks
    into the mic stream and sounds wrong.

    We push an InterruptionTaskFrame before queueing DTMF. Pipecat's task
    converts it to an InterruptionFrame downstream, which cancels any
    in-progress TTS. Timing caveat: Anthropic streams text blocks BEFORE
    the tool_use block, so by the time our handler runs some of the text
    may already have been synthesized and sent to the transport. The
    interrupt still cuts whatever remains. In practice, with rule #21 in
    the prompt telling Claude to emit at most one filler word ("sure"),
    the audible leak is ~300ms.

    ── Result handling ──
    Success uses run_llm=False so the bot stays silent after pressing —
    the IVR will respond and that response becomes the next user turn.
    Error uses default run_llm=True so Claude can react to failures
    (retry with different field, tell the rep, etc.).
    """
    digits_str = params.arguments.get("digits", "")

    if not digits_str:
        await params.result_callback({"status": "error", "message": "No digits provided"})
        return

    invalid = [c for c in digits_str if c not in _VALID_DTMF]
    if invalid:
        await params.result_callback({
            "status": "error",
            "message": f"Invalid digit(s): {''.join(invalid)}. Allowed: 0-9, *, #",
        })
        return

    # Cut any in-progress TTS speech immediately. InterruptionTaskFrame is
    # pushed upstream; the PipelineTask converts it to an InterruptionFrame
    # downstream which cancels TTS, flushes buffers, and emits BotStopped.
    try:
        await task.queue_frame(InterruptionTaskFrame())
    except Exception:
        logger.exception("press_digit: failed to push InterruptionTaskFrame")

    for char in digits_str:
        await task.queue_frame(OutputDTMFUrgentFrame(button=KeypadEntry(char)))

    logger.info(f"DTMF sent: {digits_str} (with TTS interrupt)")
    await params.result_callback(
        {"status": "success", "message": f"Pressed: {digits_str}"},
        properties=FunctionCallResultProperties(run_llm=False),
    )


TOOL_HANDLERS: dict[str, callable] = {
    "end_call": end_call,
    "transfer_call": transfer_call,
    "press_digit": press_digit,
}
