"""Pipeline builder — constructs a Pipecat pipeline from an AgentConfig."""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.turns.user_mute.always_user_mute_strategy import AlwaysUserMuteStrategy

from core.config_loader import AgentConfig
from core.hydrator import hydrate_prompt


TOOL_SCHEMAS = {
    "end_call": FunctionSchema(
        name="end_call",
        description=(
            "Call this function only when the conversation is finished, "
            "the billing inquiry is resolved, or the user says goodbye."
        ),
        properties={},
        required=[],
    ),
    "press_digit": FunctionSchema(
        name="press_digit",
        description="Press a digit on the phone keypad during an IVR menu.",
        properties={
            "digits": {
                "type": "string",
                "description": "The digit(s) to press, e.g. '1' or '1234567890'",
            }
        },
        required=["digits"],
    ),
    "transfer_call": FunctionSchema(
        name="transfer_call",
        description="Transfer the call to another department.",
        properties={
            "target": {
                "type": "string",
                "description": "The transfer target name, e.g. 'billing' or 'supervisor'",
            }
        },
        required=["target"],
    ),
}


def _create_stt(config: AgentConfig):
    if config.stt.provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService
        return DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    raise ValueError(f"Unsupported STT provider: {config.stt.provider}")


def _create_llm(config: AgentConfig):
    if config.llm.provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService
        return AnthropicLLMService(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            model=config.llm.model,
            params=AnthropicLLMService.InputParams(
                enable_prompt_caching=config.llm.enable_prompt_caching,
                max_tokens=config.llm.max_tokens,
                temperature=config.llm.temperature,
            ),
        )
    if config.llm.provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService
        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=config.llm.model,
        )
    raise ValueError(f"Unsupported LLM provider: {config.llm.provider}")


def _create_tts(config: AgentConfig):
    if config.tts.provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
        s = config.tts.settings
        return ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            voice_id=config.tts.voice_id,
            model=config.tts.model,
            params=ElevenLabsTTSService.InputParams(
                stability=s.stability,
                similarity_boost=s.similarity_boost,
                style=s.style,
                use_speaker_boost=s.use_speaker_boost,
                speed=s.speed,
            ),
        )
    if config.tts.provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService
        return CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id=config.tts.voice_id,
            model=config.tts.model,
        )
    raise ValueError(f"Unsupported TTS provider: {config.tts.provider}")


def _build_user_aggregator_params(
    config: AgentConfig,
    transport_type: Literal["twilio", "livekit"],
) -> LLMUserAggregatorParams:
    vad = SileroVADAnalyzer()

    mute_strategies = []
    if transport_type == "twilio":
        mute_strategies.append(AlwaysUserMuteStrategy())

    return LLMUserAggregatorParams(
        vad_analyzer=vad,
        user_mute_strategies=mute_strategies,
    )


def _load_agent_tools(config: AgentConfig, llm, task: PipelineTask):
    """Load and register tool handlers, injecting task for frame queueing."""
    module = importlib.import_module(config.tools_module)
    for tool_name in config.tools:
        handler = getattr(module, tool_name, None)
        if handler:
            def _make_wrapper(_h=handler, _t=task):
                async def wrapper(params):
                    return await _h(params, _t)
                return wrapper
            llm.register_function(tool_name, _make_wrapper())


def _build_tool_schemas(config: AgentConfig) -> ToolsSchema:
    """Build ToolsSchema from the agent config's tool list."""
    schemas = [TOOL_SCHEMAS[t] for t in config.tools if t in TOOL_SCHEMAS]
    return ToolsSchema(standard_tools=schemas)


class PipelineComponents:
    """Container for all pipeline components, letting the caller wire transport."""

    def __init__(self, stt, llm, tts, context_aggregator, tools):
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.context_aggregator = context_aggregator
        self.tools = tools


@dataclass
class PipelineBundle:
    """Return value of ``build_pipeline`` — everything a call handler needs."""

    pipeline: Pipeline
    task: PipelineTask
    transcript_log: list[dict[str, Any]] = field(default_factory=list)
    audiobuffer: AudioBufferProcessor | None = None


def build_pipeline_components(
    config: AgentConfig,
    case_data: dict,
    transport_type: Literal["twilio", "livekit"] = "twilio",
) -> PipelineComponents:
    """Build all pipeline components from an agent config and case data.

    The transport_type controls echo suppression (Twilio needs mute during bot
    speech) and is used to configure the user aggregator appropriately.
    The caller is responsible for creating the transport itself.
    """
    prompt = hydrate_prompt(config.prompt_path, case_data)

    stt = _create_stt(config)
    llm = _create_llm(config)
    tts = _create_tts(config)
    tools = _build_tool_schemas(config)

    messages = [
        {"role": "system", "content": prompt},
    ]
    if config.first_message:
        messages.append({"role": "assistant", "content": config.first_message})

    context = LLMContext(messages=messages, tools=tools)
    user_params = _build_user_aggregator_params(config, transport_type)
    context_aggregator = LLMContextAggregatorPair(context, user_params=user_params)

    return PipelineComponents(
        stt=stt,
        llm=llm,
        tts=tts,
        context_aggregator=context_aggregator,
        tools=tools,
    )


def build_pipeline(
    components: PipelineComponents,
    transport,
    config: AgentConfig,
    pipeline_params: PipelineParams | None = None,
    call_id: str | None = None,
) -> PipelineBundle:
    """Assemble a complete pipeline from pre-built components and a transport.

    Pass pipeline_params to override sample rates (e.g. 8kHz for Twilio).
    When ENABLE_WHISKER=true, attaches the Whisker debug observer.

    Returns a ``PipelineBundle`` with ``pipeline``, ``task``,
    ``transcript_log`` (mutable list populated by turn-stopped events), and
    ``audiobuffer`` (None when ``config.recording.enabled`` is False; otherwise
    call ``await audiobuffer.start_recording()`` after the client connects).
    """
    transcript_log: list[dict] = []

    @components.context_aggregator.user().event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(_agg, _strategy, message: UserTurnStoppedMessage):
        transcript_log.append(
            {
                "role": "user",
                "content": message.content,
                "timestamp": message.timestamp,
            }
        )

    @components.context_aggregator.assistant().event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(_agg, message: AssistantTurnStoppedMessage):
        transcript_log.append(
            {
                "role": "assistant",
                "content": message.content,
                "timestamp": message.timestamp,
            }
        )

    out_sr = None
    if pipeline_params and getattr(pipeline_params, "audio_out_sample_rate", None):
        out_sr = pipeline_params.audio_out_sample_rate

    audiobuffer: AudioBufferProcessor | None = None
    if config.recording.enabled:
        audiobuffer = AudioBufferProcessor(
            num_channels=config.recording.channels,
            sample_rate=out_sr,
            buffer_size=0,
        )

    pipeline_stages = [
        transport.input(),
        components.stt,
        components.context_aggregator.user(),
        components.llm,
        components.tts,
        transport.output(),
    ]
    if audiobuffer is not None:
        pipeline_stages.append(audiobuffer)
    pipeline_stages.append(components.context_aggregator.assistant())

    pipeline = Pipeline(pipeline_stages)

    task = PipelineTask(pipeline, params=pipeline_params or PipelineParams())

    _load_agent_tools(config, components.llm, task)

    if os.getenv("ENABLE_WHISKER", "false").lower() == "true":
        from pipecat_whisker import WhiskerObserver

        file_name = f"data/calls/{call_id}/whisker.bin" if call_id else None
        if file_name:
            os.makedirs(os.path.dirname(file_name), exist_ok=True)
        task.add_observer(WhiskerObserver(pipeline, file_name=file_name))

    return PipelineBundle(
        pipeline=pipeline,
        task=task,
        transcript_log=transcript_log,
        audiobuffer=audiobuffer,
    )
