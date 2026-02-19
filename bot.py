"""
Pipecat Voice Agent MVP — LiveKit WebRTC Transport
---------------------------------------------------
A production-ready real-time voice AI agent using:
  - LiveKit    (WebRTC transport)
  - Deepgram   (Speech-to-Text)
  - OpenAI     (LLM — gpt-4o-mini with prompt caching)
  - Cartesia   (Text-to-Speech)
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask
from pipecat.pipeline.runner import PipelineRunner
from pipecat.transports.livekit.transport import LiveKitTransport, LiveKitParams
from pipecat.runner.livekit import configure
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMContextAggregatorPair,
)
from pipecat.utils.context.llm_context_summarization import LLMContextSummarizationConfig
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndTaskFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

# Load environment variables from .env
load_dotenv()

# Project root for resolving data files
ROOT_DIR = Path(__file__).parent


def get_hydrated_prompt(case_id: str) -> tuple[str, str]:
    """Load a case from cases.json and inject its values into the system prompt.

    Args:
        case_id: The key in cases.json to look up (e.g. "case_001").

    Returns:
        A tuple of (hydrated_prompt_string, patient_name).
    """
    # Load the case data
    cases_path = ROOT_DIR / "cases.json"
    with open(cases_path) as f:
        cases = json.load(f)
    case_data = cases[case_id]

    # Load the raw system prompt
    prompt_path = ROOT_DIR / "system_prompt.txt"
    prompt = prompt_path.read_text()

    # Inject current time
    prompt = prompt.replace(
        "{{current_time}}", datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
    )

    # Replace all {{placeholder}} tags with case values
    for key, value in case_data.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", value)

    return prompt, case_data["Patient_Name"]


async def end_call(params: FunctionCallParams):
    """Tool handler: Carla hangs up the call."""
    print("Carla is hanging up the call...")
    await params.llm.queue_frame(EndTaskFrame(), FrameDirection.UPSTREAM)


async def main():
    # --- LiveKit: get room URL and token from env vars ---
    (url, token, room_name) = await configure()

    # --- Transport: LiveKit WebRTC with tuned VAD ---
    transport = LiveKitTransport(
        url=url,
        token=token,
        room_name=room_name,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    confidence=0.5,   # Lower threshold — less sensitive to background noise
                    start_secs=0.2,   # Require 200ms of speech before triggering interruption
                    stop_secs=0.8,    # Wait 800ms of silence before cutting off the speaker
                )
            ),
            vad_audio_passthrough=True,
        ),
    )

    # --- Speech-to-Text: Deepgram ---
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

    # --- LLM: OpenAI gpt-4o-mini (prompt caching + better tool handling) ---
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-5.2-2025-12-11",
    )

    # Register the end_call tool so the LLM can invoke it
    llm.register_function("end_call", end_call)

    # --- Text-to-Speech: Cartesia (fastest model) ---
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id=os.getenv("CARTESIA_VOICE_ID"),
        model="sonic-3",
    )

    # --- LLM Context: hydrate system prompt with case data ---
    system_prompt, patient_name = get_hydrated_prompt("case_001")

    messages = [
        {
            "role": "system",
            "content": system_prompt,
        }
    ]

    # --- Tool definitions for the LLM ---
    end_call_tool = FunctionSchema(
        name="end_call",
        description=(
            "Call this function only when the conversation is finished, "
            "the billing inquiry is resolved, or the user says goodbye."
        ),
        properties={},
        required=[],
    )
    tools = ToolsSchema(standard_tools=[end_call_tool])

    context = LLMContext(messages=messages, tools=tools)
    context_aggregator = LLMContextAggregatorPair(
        context,
        assistant_params=LLMAssistantAggregatorParams(
            enable_context_summarization=True,
            context_summarization_config=LLMContextSummarizationConfig(
                max_unsummarized_messages=10,  # Keep last 10 messages before summarizing
            ),
        ),
    )

    # --- Pipeline: wire everything together in order ---
    pipeline = Pipeline(
        [
            transport.input(),              # LiveKit audio in
            stt,                            # Speech-to-Text
            context_aggregator.user(),      # Collect user transcription
            llm,                            # LLM inference
            tts,                            # Text-to-Speech
            transport.output(),             # LiveKit audio out
            context_aggregator.assistant(), # Collect assistant response
        ]
    )

    task = PipelineTask(pipeline)

    # --- Event: greet user when the first participant joins ---
    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant_id):
        print("Participant joined. Bot is listening...")

    # --- Run the pipeline ---
    runner = PipelineRunner()
    await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
