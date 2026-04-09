"""Pipeline builder — constructs a Pipecat pipeline from an AgentConfig."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger
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


TOOL_PROPERTIES: dict[str, dict] = {
    "end_call": {"properties": {}, "required": []},
    "press_digit": {
        "properties": {
            "digits": {
                "type": "string",
                "description": "The digit(s) to press, e.g. '1' or '1234567890'",
            },
        },
        "required": ["digits"],
    },
    "transfer_call": {
        "properties": {
            "target": {
                "type": "string",
                "description": "The transfer target name",
            },
        },
        "required": ["target"],
    },
}

_GLOBAL_SIMILARITY_BOOST = 0.8


def _create_stt(config: AgentConfig):
    if config.stt.provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        stt_settings = None
        if (config.stt.language and config.stt.language != "en") or config.stt.keywords:
            settings_kwargs: dict[str, Any] = {}
            if config.stt.language and config.stt.language != "en":
                settings_kwargs["language"] = config.stt.language
            if config.stt.keywords:
                settings_kwargs["keywords"] = config.stt.keywords
            stt_settings = DeepgramSTTService.Settings(**settings_kwargs)

        logger.debug(
            f"STT: provider=deepgram language={config.stt.language} "
            f"keywords={config.stt.keywords}"
        )
        return DeepgramSTTService(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            settings=stt_settings,
        )
    raise ValueError(f"Unsupported STT provider: {config.stt.provider}")


def _create_llm(config: AgentConfig):
    if config.llm.provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        logger.debug(
            f"LLM: provider=anthropic model={config.llm.model} "
            f"temp={config.llm.temperature} max_tokens={config.llm.max_tokens} "
            f"prompt_caching=True (hardcoded)"
        )
        return AnthropicLLMService(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            model=config.llm.model,
            params=AnthropicLLMService.InputParams(
                enable_prompt_caching=True,
                max_tokens=config.llm.max_tokens,
                temperature=config.llm.temperature,
            ),
        )
    if config.llm.provider == "openai":
        from pipecat.services.openai.llm import OpenAILLMService

        logger.debug(f"LLM: provider=openai model={config.llm.model}")
        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=config.llm.model,
        )
    raise ValueError(f"Unsupported LLM provider: {config.llm.provider}")


def _create_tts(config: AgentConfig):
    if config.tts.provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        s = config.tts.settings
        logger.debug(
            f"TTS: provider=elevenlabs voice={config.tts.voice_id} "
            f"model={config.tts.model} stability={s.stability} "
            f"similarity_boost={_GLOBAL_SIMILARITY_BOOST} (global) "
            f"style={s.style} speaker_boost={s.use_speaker_boost} speed={s.speed}"
        )
        return ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            settings=ElevenLabsTTSService.Settings(
                voice=config.tts.voice_id,
                model=config.tts.model,
                stability=s.stability,
                similarity_boost=_GLOBAL_SIMILARITY_BOOST,
                style=s.style,
                use_speaker_boost=s.use_speaker_boost,
                speed=s.speed,
            ),
        )
    if config.tts.provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        logger.debug(
            f"TTS: provider=cartesia voice={config.tts.voice_id} model={config.tts.model}"
        )
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
    """Register tool handlers from the central TOOL_HANDLERS registry."""
    from core.tool_handlers import TOOL_HANDLERS

    for tool in config.tools:
        handler = TOOL_HANDLERS.get(tool.type)
        if handler:
            tool_settings = tool.settings

            def _make_wrapper(_h=handler, _t=task, _s=tool_settings):
                async def wrapper(params):
                    return await _h(params, _t, _s)
                return wrapper
            llm.register_function(tool.type, _make_wrapper())


def _build_tool_schemas(config: AgentConfig) -> ToolsSchema:
    """Build ToolsSchema from the agent config's tool objects.

    Each tool has a type, description, and settings. For transfer_call,
    injects the available target names as an enum so the LLM can only
    pick valid targets.
    """
    schemas: list[FunctionSchema] = []
    for tool in config.tools:
        base = TOOL_PROPERTIES.get(tool.type)
        if base is None:
            continue

        properties = dict(base["properties"])

        if tool.type == "transfer_call":
            targets = tool.settings.get("targets", {})
            if targets:
                target_names = sorted(targets.keys())
                properties = {
                    "target": {
                        "type": "string",
                        "enum": target_names,
                        "description": f"Transfer target: {', '.join(target_names)}",
                    },
                }

        schemas.append(FunctionSchema(
            name=tool.type,
            description=tool.description,
            properties=properties,
            required=base["required"],
        ))
    return ToolsSchema(standard_tools=schemas)


def _load_global_recording_config() -> tuple[bool, int]:
    """Read recording settings from global_settings table.

    Returns (enabled, channels). Defaults to (True, 2) if not found.
    """
    try:
        from core.supabase_client import get_supabase

        resp = (
            get_supabase()
            .table("global_settings")
            .select("value")
            .eq("key", "recording")
            .limit(1)
            .execute()
        )
        if resp.data:
            val = resp.data[0]["value"]
            return val.get("enabled", True), val.get("channels", 2)
    except Exception:
        logger.warning("Failed to load global recording config, using defaults")
    return True, 2


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
    prompt = hydrate_prompt(config.system_prompt, case_data)

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
    rtvi_observer_params=None,
) -> PipelineBundle:
    """Assemble a complete pipeline from pre-built components and a transport.

    Pass pipeline_params to override sample rates (e.g. 8kHz for Twilio).
    When ENABLE_WHISKER=true, attaches the Whisker debug observer.

    Recording config is read from global_settings, not per-agent config.
    """
    transcript_log: list[dict] = []
    _MERGE_WINDOW_SECS = 2.0

    def _append_transcript(role: str, content: str, timestamp: str):
        content = content.strip()
        if not content:
            return

        if transcript_log:
            last = transcript_log[-1]

            if last["role"] == role and last["content"].strip() == content:
                return

            if last["role"] == role and content in last["content"]:
                return

            try:
                from datetime import datetime as _dt
                last_t = _dt.fromisoformat(last["timestamp"])
                new_t = _dt.fromisoformat(timestamp)
                delta = abs((new_t - last_t).total_seconds())
            except (ValueError, TypeError):
                delta = _MERGE_WINDOW_SECS + 1

            if last["role"] == role and delta < _MERGE_WINDOW_SECS:
                if content not in last["content"]:
                    last["content"] = last["content"].rstrip() + " " + content.lstrip()
                    last["timestamp"] = timestamp
                return

        transcript_log.append({"role": role, "content": content, "timestamp": timestamp})

    @components.context_aggregator.user().event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(_agg, _strategy, message: UserTurnStoppedMessage):
        _append_transcript("user", message.content, message.timestamp)

    @components.context_aggregator.assistant().event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(_agg, message: AssistantTurnStoppedMessage):
        _append_transcript("assistant", message.content, message.timestamp)

    out_sr = None
    if pipeline_params and getattr(pipeline_params, "audio_out_sample_rate", None):
        out_sr = pipeline_params.audio_out_sample_rate

    rec_enabled, rec_channels = _load_global_recording_config()
    logger.debug(f"Recording: enabled={rec_enabled} channels={rec_channels} (global)")

    audiobuffer: AudioBufferProcessor | None = None
    if rec_enabled:
        audiobuffer = AudioBufferProcessor(
            num_channels=rec_channels,
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

    task = PipelineTask(
        pipeline,
        params=pipeline_params or PipelineParams(),
        rtvi_observer_params=rtvi_observer_params,
    )

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
