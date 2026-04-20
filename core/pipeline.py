"""Pipeline builder — constructs a Pipecat pipeline from an AgentConfig."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
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
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

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
    # Note on DTMF: press_digit (above) is the tool-call path used by
    # conversation-first agents (ivr_goal empty). Agents with ivr_goal set
    # get DTMF via Pipecat's IVRNavigator using <dtmf>N</dtmf> XML tags
    # embedded in LLM output (no tool call). Both paths coexist.
}

# Turn-taking and VAD tuning.
# These can be overridden per-environment via env vars without a code change.
# Smart-turn stop_secs: time of trailing silence before committing a user turn.
# Pipecat default is 3.0s (feels like the bot froze). 0.8s is aggressive but
# works because Smart Turn v3 is semantic, not silence-based — stop_secs is
# a fallback timeout rather than the primary signal.
SMART_TURN_STOP_SECS = float(os.getenv("SMART_TURN_STOP_SECS", "0.8"))
# SileroVAD min_volume: floor for speech-energy detection. Pipecat default is
# 0.6; raising to 0.75 prevents background noise / carrier hiss from being
# transcribed as phantom user turns.
VAD_MIN_VOLUME = float(os.getenv("VAD_MIN_VOLUME", "0.75"))
VAD_CONFIDENCE = float(os.getenv("VAD_CONFIDENCE", "0.7"))
VAD_START_SECS = float(os.getenv("VAD_START_SECS", "0.2"))
VAD_STOP_SECS = float(os.getenv("VAD_STOP_SECS", "0.2"))


def _create_stt(config: AgentConfig):
    provider = config.stt.provider

    if provider == "deepgram":
        # Deepgram Nova STT — our known-good baseline. Handles both 8kHz and
        # 16kHz input transparently (rate is picked up from StartFrame via
        # the pipeline's audio_in_sample_rate).
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

    if provider == "elevenlabs":
        # ElevenLabs Scribe Realtime — kept as a fallback. Deliberately does
        # NOT pin sample_rate; lets the pipeline's audio_in_sample_rate
        # drive the rate so Scribe and our Twilio serializer agree.
        from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService

        settings_kwargs: dict[str, Any] = {}
        if config.stt.language:
            settings_kwargs["language"] = config.stt.language
        stt_settings = (
            ElevenLabsRealtimeSTTService.Settings(**settings_kwargs)
            if settings_kwargs
            else None
        )

        logger.debug(
            f"STT: provider=elevenlabs language={config.stt.language} "
            f"keywords={config.stt.keywords or []} (keywords unused by ElevenLabs Scribe)"
        )
        return ElevenLabsRealtimeSTTService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            settings=stt_settings,
        )

    raise ValueError(f"Unsupported STT provider: {provider}")


def _create_llm(config: AgentConfig):
    if config.llm.provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        # Prompt caching is always on: 34K-char system prompts make cold calls
        # cost ~1s; warm-cache reads are ~650ms. There is no scenario where
        # we'd want this off, so it's hardcoded rather than agent-configurable.
        logger.debug(
            f"LLM: provider=anthropic model={config.llm.model} "
            f"temp={config.llm.temperature} max_tokens={config.llm.max_tokens} "
            f"prompt_caching=True"
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
    provider = config.tts.provider

    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        s = config.tts.settings
        logger.debug(
            f"TTS: provider=elevenlabs voice={config.tts.voice_id} "
            f"model={config.tts.model} stability={s.stability} "
            f"similarity_boost={s.similarity_boost} "
            f"style={s.style} speaker_boost={s.use_speaker_boost} speed={s.speed}"
        )
        return ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            settings=ElevenLabsTTSService.Settings(
                voice=config.tts.voice_id,
                model=config.tts.model,
                stability=s.stability,
                similarity_boost=s.similarity_boost,
                style=s.style,
                use_speaker_boost=s.use_speaker_boost,
                speed=s.speed,
            ),
        )

    if provider == "fish":
        # Fish Audio — hosted API for now; will move to self-hosted on EC2 GPU
        # once we validate voice quality.
        #
        # Pipecat Fish API gotchas:
        #   - `voice` in Settings is the Fish "reference_id" (their voice
        #     handle). Maps from config.tts.voice_id.
        #   - `model` in Settings is the Fish TTS model (s1, s2, s2-pro, etc.)
        #     — NOT the voice. Separate axes.
        #   - Top-level `model=` kwarg is deprecated and aliases reference_id,
        #     which collides and raises. Always use settings=Settings(...).
        #
        # Fish-API-supported latency values are only "normal" (~500ms) and
        # "balanced" (~300ms). "low" is NOT valid per Fish docs — it's
        # silently ignored server-side and falls back to "normal".
        #
        # Sample rate: we deliberately do NOT pass sample_rate=N here. Fish
        # reads self.sample_rate at start() time, which comes from the
        # pipeline's audio_out_sample_rate. For Twilio (ws_handler) this is
        # 8000; for WebRTC (webrtc_handler) it defaults to Pipecat's 24000.
        # Forcing 8kHz synthesis uses a degraded path on Fish's side, so
        # we rely on the pipeline to set the right rate per transport.
        #
        # output_format and latency are env-tunable for A/B testing without
        # a redeploy (FISH_OUTPUT_FORMAT in {pcm, mp3, wav, opus};
        # FISH_LATENCY in {normal, balanced}).
        # Use our patched subclass instead of Pipecat's stock service.
        # See core/pipecat_patches.py for the _receive_messages override
        # that fixes the NATO-spelling silence bug (1024-byte chunk filter
        # eating short phonetic fragments on 8kHz Twilio audio).
        from core.pipecat_patches import FishAudioTTSServicePatched as FishAudioTTSService

        s = config.tts.settings

        raw_latency = os.getenv("FISH_LATENCY", "balanced")
        latency_mode = raw_latency if raw_latency in ("normal", "balanced") else "balanced"
        if latency_mode != raw_latency:
            logger.warning(
                f"FISH_LATENCY={raw_latency!r} is not a supported Fish mode "
                f"(valid: 'normal', 'balanced') — falling back to 'balanced'"
            )

        # Only "pcm" is currently safe. Pipecat's Fish integration wraps the
        # raw audio bytes straight into TTSAudioRawFrame without decoding, so
        # mp3/wav/opus would be fed to the transport as if they were PCM
        # samples and produce silence/garbage. Keeping the env var so we can
        # revisit if Pipecat adds decoding upstream.
        raw_format = os.getenv("FISH_OUTPUT_FORMAT", "pcm").lower()
        if raw_format != "pcm":
            logger.warning(
                f"FISH_OUTPUT_FORMAT={raw_format!r} is not usable with Pipecat "
                f"0.0.108 (no decoder for non-pcm formats) — forcing 'pcm'"
            )
        output_format = "pcm"

        fish_model = config.tts.model or "s2-pro"

        logger.info(
            f"[Fish TTS] provider=fish voice={config.tts.voice_id} "
            f"model={fish_model} latency={latency_mode} speed={s.speed} "
            f"output_format={output_format} "
            f"sample_rate=<pipeline.audio_out_sample_rate — logged at StartFrame>"
        )

        service = FishAudioTTSService(
            api_key=os.getenv("FISH_API_KEY"),
            output_format=output_format,
            # sample_rate intentionally omitted — see comment above.
            settings=FishAudioTTSService.Settings(
                voice=config.tts.voice_id,
                model=fish_model,
                latency=latency_mode,
                prosody_speed=s.speed if s.speed is not None else 1.0,
            ),
        )

        # Wrap the service's start() so we can log the sample rate Fish is
        # actually told to use (only known once StartFrame arrives). This is
        # the exact rate Fish synthesizes at — if it's 8000 you're on the
        # degraded path; 16000+ is the good path.
        _orig_start = service.start

        async def _start_with_logging(frame):
            await _orig_start(frame)
            logger.info(
                f"[Fish TTS] negotiated sample_rate={service.sample_rate}Hz "
                f"(sent to Fish in start message; Fish synthesizes at this rate)"
            )

        service.start = _start_with_logging
        return service

    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        logger.debug(
            f"TTS: provider=cartesia voice={config.tts.voice_id} model={config.tts.model}"
        )
        return CartesiaTTSService(
            api_key=os.getenv("CARTESIA_API_KEY"),
            voice_id=config.tts.voice_id,
            model=config.tts.model,
        )

    raise ValueError(f"Unsupported TTS provider: {provider}")


def _build_user_aggregator_params(
    config: AgentConfig,
    transport_type: Literal["twilio", "livekit"],
) -> LLMUserAggregatorParams:
    # Silero VAD — raising min_volume above the default 0.6 keeps carrier
    # hiss / ringback noise from registering as speech and producing phantom
    # user turns before the human says anything.
    vad = SileroVADAnalyzer(
        params=VADParams(
            confidence=VAD_CONFIDENCE,
            start_secs=VAD_START_SECS,
            stop_secs=VAD_STOP_SECS,
            min_volume=VAD_MIN_VOLUME,
        )
    )

    # Smart-turn — explicitly constructed with a shorter stop_secs. Without
    # this, Pipecat auto-defaults to LocalSmartTurnAnalyzerV3() with
    # stop_secs=3.0, which forces 3 full seconds of silence before the bot
    # responds — the single biggest contributor to a sluggish feel.
    #
    # Turn-start strategy: VAD-only. The Pipecat default also includes
    # TranscriptionUserTurnStartStrategy, which opens a new user turn on every
    # incoming interim/final transcript. With ElevenLabs Scribe this creates
    # a feedback loop: Scribe commits a segment → transcript frame arrives →
    # a fresh "user is speaking" turn opens → that empty turn times out
    # without a transcript, blocking the LLM for 10+ seconds. Dropping the
    # transcription-based start strategy keeps turn-start VAD-driven and
    # eliminates the dead zones observed on 2026-04-16.
    turn_analyzer = LocalSmartTurnAnalyzerV3(
        params=SmartTurnParams(stop_secs=SMART_TURN_STOP_SECS),
    )
    turn_strategies = UserTurnStrategies(
        start=[VADUserTurnStartStrategy()],
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=turn_analyzer)],
    )

    logger.debug(
        f"Turn-taking: vad_min_volume={VAD_MIN_VOLUME} "
        f"vad_confidence={VAD_CONFIDENCE} "
        f"smart_turn_stop_secs={SMART_TURN_STOP_SECS}"
    )

    mute_strategies = []
    if transport_type == "twilio":
        mute_strategies.append(AlwaysUserMuteStrategy())

    return LLMUserAggregatorParams(
        vad_analyzer=vad,
        user_turn_strategies=turn_strategies,
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


def _get_recording_config(config: "AgentConfig") -> tuple[bool, int]:
    """Read recording settings from the agent config.

    Returns (enabled, channels). Defaults to (True, 2).
    """
    return config.recording.enabled, config.recording.channels


class PipelineComponents:
    """Container for all pipeline components, letting the caller wire transport.

    ``ivr_navigator`` is optional. When present it wraps ``llm`` (Pipecat's
    IVRNavigator is a Pipeline subclass of [llm, ivr_processor]) and is the
    processor that goes into the outer pipeline in the LLM's slot. ``llm``
    is still retained on this object so tool registration and cache prewarm
    can operate on the underlying service.
    """

    def __init__(self, stt, llm, tts, context_aggregator, tools, ivr_navigator=None):
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.context_aggregator = context_aggregator
        self.tools = tools
        self.ivr_navigator = ivr_navigator


@dataclass
class PipelineBundle:
    """Return value of ``build_pipeline`` — everything a call handler needs."""

    pipeline: Pipeline
    task: PipelineTask
    transcript_log: list[dict[str, Any]] = field(default_factory=list)
    audiobuffer: AudioBufferProcessor | None = None


async def _prewarm_anthropic_cache(
    llm_service,
    hydrated_prompt: str,
    tools,
) -> None:
    """Fire a 1-token Anthropic call to prime the prompt cache.

    Uses Pipecat's AnthropicLLMService.run_inference so the system prompt,
    tools, and cache_control placement are byte-identical to what the real
    first-turn LLM call will send. Anthropic caches by exact content match;
    if we hand-roll the call we risk a silent cache miss on the real turn
    (different tools schema serialization, different cache_control block
    layout, etc.).

    Expected savings: ~3.1s on the first-turn LLM TTFB
    (cold ~3.7s → warm ~0.6s). Cost: one cache write per call (~$0.003).

    Caveats:
    - Anthropic's ephemeral cache TTL is 5 minutes. Fine for typical calls.
    - If the real turn adds tokens to the system prompt or tools list in a
      way that shifts the prefix, cache won't hit — but the `cache_read`
      token count on turn 1 will tell us definitively.
    - Fire-and-forget: errors are logged but don't block pipeline startup.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        return
    try:
        # Build a minimal context identical to the real first turn except
        # we append a single "." user message so Anthropic accepts the call.
        # System prompt and tools are the exact same Python objects that will
        # be on the live context — no re-serialization drift.
        prewarm_messages = [
            {"role": "system", "content": hydrated_prompt},
            {"role": "user", "content": "."},
        ]
        prewarm_context = LLMContext(messages=prewarm_messages, tools=tools)
        # max_tokens=1 minimizes cost; we only want the cache write to land.
        await llm_service.run_inference(prewarm_context, max_tokens=1)
        logger.info(
            f"[prewarm] Anthropic cache primed via run_inference "
            f"({len(hydrated_prompt)} chars, {len(getattr(tools, 'standard_tools', []))} tools)"
        )
    except Exception as e:
        # Non-fatal — the call will just pay the cold-cache cost on turn 1.
        logger.warning(f"[prewarm] Anthropic cache prime failed: {e}")


def build_pipeline_components(
    config: AgentConfig,
    case_data: dict,
    transport_type: Literal["twilio", "livekit"] = "twilio",
) -> PipelineComponents:
    """Build all pipeline components from an agent config and case data.

    The transport_type controls echo suppression (Twilio needs mute during bot
    speech) and is used to configure the user aggregator appropriately.
    The caller is responsible for creating the transport itself.

    IVR navigation — opt-in per agent:
    - If ``config.ivr_goal`` is set, wraps the LLM in a Pipecat IVRNavigator.
      The navigator starts in classifier mode on the first user turn:
        * IVR menu detected → autonomous navigation via <dtmf> XML tags
        * Human detected    → fires on_conversation_detected, we swap to the
          conversation system_prompt (handled in build_pipeline()).
    - If ``config.ivr_goal`` is empty/None, the pipeline is the standard
      LLM-only flow (no navigator, no classifier call). Existing agents
      keep working exactly as before.

    Cache prewarm:
    - Without IVR: fires immediately with the conversation prompt so the
      first real user turn reads from cache (~0.6s TTFB instead of ~3.7s).
    - With IVR: deferred. The pipeline's initial LLM call uses the
      classifier prompt which is small and fast anyway; the conversation
      prompt is only needed after on_conversation_detected, at which point
      build_pipeline's handler fires the prewarm.
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

    # IVR navigator (opt-in). IVR mode uses stop_secs=2.0 by default — IVR
    # menus often have pauses between prompts, so we need to wait longer
    # before deciding a turn is over. Conversation mode reverts to our
    # tuned SMART_TURN_STOP_SECS via the on_conversation_detected handler.
    ivr_navigator = None
    if config.ivr_goal and config.ivr_goal.strip():
        from pipecat.audio.vad.vad_analyzer import VADParams as _VADParams
        from pipecat.extensions.ivr.ivr_navigator import IVRNavigator

        hydrated_goal = hydrate_prompt(config.ivr_goal, case_data)
        ivr_navigator = IVRNavigator(
            llm=llm,
            ivr_prompt=hydrated_goal,
            ivr_vad_params=_VADParams(stop_secs=2.0),
        )
        logger.info(
            f"IVRNavigator enabled for {config.name} — "
            f"goal={len(hydrated_goal)} chars"
        )

    # Cache prewarm — only fire immediately when there is NO navigator.
    # With navigator the first LLM call uses the classifier prompt (small),
    # not the 35K-char conversation prompt, so prewarming the conversation
    # prompt now is wasted work; we fire it after the handoff instead.
    if config.llm.provider == "anthropic" and ivr_navigator is None:
        try:
            import asyncio

            asyncio.create_task(
                _prewarm_anthropic_cache(llm, prompt, tools)
            )
        except RuntimeError:
            logger.debug("[prewarm] No running loop, skipping")

    return PipelineComponents(
        stt=stt,
        llm=llm,
        tts=tts,
        context_aggregator=context_aggregator,
        tools=tools,
        ivr_navigator=ivr_navigator,
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

    rec_enabled, rec_channels = _get_recording_config(config)
    logger.debug(f"Recording: enabled={rec_enabled} channels={rec_channels}")

    audiobuffer: AudioBufferProcessor | None = None
    if rec_enabled:
        audiobuffer = AudioBufferProcessor(
            num_channels=rec_channels,
            sample_rate=out_sr,
            buffer_size=0,
        )

    # When IVRNavigator is active it goes in the LLM's slot; the navigator
    # is itself a Pipeline containing [llm, ivr_processor] so the LLM is
    # still the one doing inference downstream of it.
    llm_stage = components.ivr_navigator or components.llm

    pipeline_stages = [
        transport.input(),
        components.stt,
        components.context_aggregator.user(),
        llm_stage,
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

    # IVRNavigator event handlers. Registered here because we need both the
    # task (for VAD param updates and LLM message replacement) and the
    # hydrated conversation prompt in scope.
    if components.ivr_navigator is not None:
        import asyncio as _asyncio
        from pipecat.audio.vad.vad_analyzer import VADParams as _VADParams
        from pipecat.frames.frames import (
            LLMMessagesUpdateFrame as _LLMMessagesUpdateFrame,
            VADParamsUpdateFrame as _VADParamsUpdateFrame,
        )

        # Pull the hydrated conversation system prompt from the context that
        # was built in build_pipeline_components — we need the case_data
        # substitutions that only happened at build time. Fall back to the
        # raw template if the context internals change in a future Pipecat.
        try:
            conv_context = components.context_aggregator.user()._context
            conv_messages = list(conv_context.messages)
            hydrated_conv_prompt = next(
                (m["content"] for m in conv_messages if m.get("role") == "system"),
                config.system_prompt,
            )
        except Exception:
            logger.warning(
                "[IVR] Could not read hydrated prompt from context; "
                "falling back to raw config.system_prompt (no case_data)"
            )
            hydrated_conv_prompt = config.system_prompt

        @components.ivr_navigator.event_handler("on_conversation_detected")
        async def _on_conversation_detected(_processor, conversation_history):
            """IVR → human transition. Swap in the conversation system prompt."""
            logger.info(
                f"[IVR] on_conversation_detected fired "
                f"({len(conversation_history or [])} history messages)"
            )
            messages = [{"role": "system", "content": hydrated_conv_prompt}]
            if conversation_history:
                messages.extend(conversation_history)
            # task.queue_frame defaults to DOWNSTREAM, which is what we want —
            # the frame flows into the pipeline and Pipecat's AnthropicLLMService
            # picks it up to replace its message list. This matches Pipecat's
            # documented IVRNavigator usage pattern.
            await task.queue_frame(
                _LLMMessagesUpdateFrame(messages=messages, run_llm=True)
            )
            # Restore conversation-phase VAD tuning (IVR used stop_secs=2.0).
            await task.queue_frame(
                _VADParamsUpdateFrame(params=_VADParams(
                    confidence=VAD_CONFIDENCE,
                    start_secs=VAD_START_SECS,
                    stop_secs=VAD_STOP_SECS,
                    min_volume=VAD_MIN_VOLUME,
                )),
            )
            # Deferred cache prewarm — now that we know the call is in
            # conversation mode, prime the cache for the real LLM turns
            # that'll follow. First-turn TTFB should drop to ~0.6s.
            if config.llm.provider == "anthropic":
                try:
                    _asyncio.create_task(
                        _prewarm_anthropic_cache(
                            components.llm, hydrated_conv_prompt, components.tools
                        )
                    )
                except RuntimeError:
                    logger.debug("[prewarm] No running loop, skipping post-IVR prewarm")

        @components.ivr_navigator.event_handler("on_ivr_status_changed")
        async def _on_ivr_status_changed(_processor, status):
            logger.info(f"[IVR] status={status}")

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
