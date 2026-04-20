"""Agent config loader — reads from Lambda API with in-memory cache."""

import asyncio
import time
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Pydantic models (pipeline interface contract) ────────────────────────────


class LLMConfig(BaseModel):
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 200
    temperature: float = 0.7
    enable_prompt_caching: bool = True


class TTSSettings(BaseModel):
    stability: Optional[float] = None
    similarity_boost: Optional[float] = None
    style: Optional[float] = None
    use_speaker_boost: Optional[bool] = None
    speed: Optional[float] = None


class TTSConfig(BaseModel):
    provider: str = "elevenlabs"
    voice_id: str = ""
    model: str = "eleven_turbo_v2_5"
    settings: TTSSettings = Field(default_factory=TTSSettings)


class STTConfig(BaseModel):
    provider: str = "elevenlabs"
    language: str = "en"
    keywords: list[str] = Field(default_factory=list)


class ToolConfig(BaseModel):
    type: str
    description: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class PostCallField(BaseModel):
    name: str
    type: str = "text"
    description: str = ""
    format_examples: list[str] = Field(default_factory=list)
    choices: list[str] = Field(default_factory=list)


class PostCallConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    fields: list[PostCallField] = Field(default_factory=list)


class RecordingConfig(BaseModel):
    enabled: bool = True
    channels: int = 2


class AgentConfig(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    system_prompt: str = ""
    first_message: str = ""
    # IVR navigation goal. When set, the pipeline wraps the LLM in an
    # IVRNavigator that classifies each call's opening audio as IVR vs
    # human, navigates any menu tree autonomously, then hands off to the
    # system_prompt for human conversation. When empty/None, the pipeline
    # runs a standard LLM-only flow (no navigator, no classifier call).
    ivr_goal: str = ""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    tools: list[ToolConfig] = Field(default_factory=list)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    post_call_analyses: PostCallConfig | None = None


# ── Row-to-config mapper ────────────────────────────────────────────────────


def row_to_config(row: dict[str, Any]) -> AgentConfig:
    pca_raw = row.get("post_call_analyses")
    pca_config: PostCallConfig | None = None
    if isinstance(pca_raw, dict) and pca_raw.get("fields"):
        pca_config = PostCallConfig(
            model=pca_raw.get("model", "claude-haiku-4-5-20251001"),
            fields=[PostCallField(**f) for f in pca_raw["fields"] if isinstance(f, dict)],
        )

    tools_raw = row.get("tools") or []
    tools = []
    for t in tools_raw:
        if isinstance(t, dict):
            tools.append(ToolConfig(
                type=t["type"],
                description=t.get("description", ""),
                settings=t.get("settings", {}),
            ))

    return AgentConfig(
        name=row["name"],
        display_name=row.get("display_name") or row["name"],
        description=row.get("description", ""),
        system_prompt=row.get("system_prompt", ""),
        first_message=row.get("first_message", ""),
        ivr_goal=row.get("ivr_goal") or "",
        llm=LLMConfig(
            provider=row.get("llm_provider", "anthropic"),
            model=row.get("llm_model", "claude-sonnet-4-6"),
            max_tokens=row.get("max_tokens", 200),
            temperature=row.get("temperature", 0.7),
            enable_prompt_caching=row.get("enable_prompt_caching", True),
        ),
        tts=TTSConfig(
            provider=row.get("tts_provider", "elevenlabs"),
            voice_id=row.get("tts_voice_id", ""),
            model=row.get("tts_model", "eleven_turbo_v2_5"),
            settings=TTSSettings(
                stability=row.get("tts_stability"),
                similarity_boost=row.get("tts_similarity_boost"),
                style=row.get("tts_style"),
                use_speaker_boost=row.get("tts_use_speaker_boost"),
                speed=row.get("tts_speed"),
            ),
        ),
        stt=STTConfig(
            provider=row.get("stt_provider") or "elevenlabs",
            language=row.get("stt_language", "en"),
            keywords=list(row.get("stt_keywords") or []),
        ),
        tools=tools,
        recording=RecordingConfig(
            enabled=row.get("recording_enabled", True),
            channels=row.get("recording_channels", 2),
        ),
        post_call_analyses=pca_config,
    )


# ── Cache ────────────────────────────────────────────────────────────────────

_cache: dict[str, tuple[float, AgentConfig]] = {}
_CACHE_TTL = 60.0


def invalidate_cache(agent_name: str | None = None):
    """Drop cached config. Pass None to clear all."""
    if agent_name:
        _cache.pop(agent_name, None)
    else:
        _cache.clear()


# ── Public API ───────────────────────────────────────────────────────────────


def load_agent_config(agent_name: str) -> AgentConfig:
    """Load agent config from Lambda (cached for 60s).

    Uses asyncio.run() when called from sync context (e.g. batch runner
    pre-validation).  The Lambda client handles float casting internally.
    """
    now = time.monotonic()
    cached = _cache.get(agent_name)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]

    from core.lambda_client import get_agent_config

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            row = pool.submit(asyncio.run, get_agent_config(agent_name)).result()
    else:
        row = asyncio.run(get_agent_config(agent_name))

    if not row:
        raise ValueError(f"Agent not found: {agent_name}")

    config = row_to_config(row)
    _cache[agent_name] = (now, config)
    return config


def load_agent_draft(agent_name: str) -> AgentConfig:
    """Load unpublished draft config for test calls."""
    from core.lambda_client import get_agent_draft

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            row = pool.submit(asyncio.run, get_agent_draft(agent_name)).result()
    else:
        row = asyncio.run(get_agent_draft(agent_name))

    if not row:
        raise ValueError(f"No draft found for agent: {agent_name}")
    return row_to_config(row)
