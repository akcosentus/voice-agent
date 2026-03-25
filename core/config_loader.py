"""Agent config loader with YAML parsing, env var resolution, and inheritance."""

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

AGENTS_DIR = Path(__file__).parent.parent / "agents"

_ENV_VAR_RE = re.compile(r"\$\{(\w+)}")


def _resolve_env_vars(value: Any) -> Any:
    """Recursively resolve ${ENV_VAR} references in strings."""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            var = match.group(1)
            return os.environ.get(var, "")
        return _ENV_VAR_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Deep merge overrides into base, returning a new dict."""
    result = deepcopy(base)
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


# --- Pydantic models ---

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
    provider: str = "deepgram"


class InteractionConfig(BaseModel):
    function_call_filler: str = ""
    no_filler_functions: list[str] = ["end_call", "transfer_call"]


class TelephonyConfig(BaseModel):
    phone_number: str = ""


class PostCallAnalysis(BaseModel):
    name: str
    model: str = "claude-haiku-4-5-20251001"
    prompt_file: str
    output_type: Literal["text", "boolean", "json"] = "text"


class RecordingConfig(BaseModel):
    enabled: bool = True
    channels: int = 2


class CaseDataConfig(BaseModel):
    source: str = "json"
    path: str = ""


class AgentConfig(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    type: str = "outbound"
    prompt_file: str = "prompt.txt"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    telephony: TelephonyConfig = Field(default_factory=TelephonyConfig)
    transfer_targets: dict[str, str] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    first_message: str = ""
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    post_call_analyses: list[PostCallAnalysis] = Field(default_factory=list)
    case_data: CaseDataConfig = Field(default_factory=CaseDataConfig)

    @property
    def agent_dir(self) -> Path:
        return AGENTS_DIR / self.name

    _base_tools_module: Optional[str] = None
    _base_prompt_path: Optional[Path] = None

    @property
    def prompt_path(self) -> Path:
        if self._base_prompt_path:
            return self._base_prompt_path
        return self.agent_dir / self.prompt_file

    @property
    def tools_module(self) -> str:
        if self._base_tools_module:
            return self._base_tools_module
        return f"agents.{self.name.replace('/', '.')}.tools"


def load_agent_config(agent_name: str) -> AgentConfig:
    """Load agent config from agents/{agent_name}/config.yaml.

    Handles 'extends' + 'overrides' inheritance for division variants,
    and resolves ${ENV_VAR} references from the environment.
    """
    config_path = AGENTS_DIR / agent_name / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Agent config not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    # Handle inheritance
    base_name = None
    if "extends" in raw:
        base_rel = raw["extends"]
        base_name = str(Path(base_rel).parent)
        base_path = AGENTS_DIR / base_rel
        with open(base_path) as f:
            base = yaml.safe_load(f)
        overrides = raw.get("overrides", {})
        raw = _deep_merge(base, overrides)
        raw["name"] = agent_name

    raw = _resolve_env_vars(raw)
    config = AgentConfig(**raw)

    if base_name:
        # If variant has no tools.py, use the base's tools module
        if not (AGENTS_DIR / agent_name / "tools.py").exists():
            config._base_tools_module = f"agents.{base_name.replace('/', '.')}.tools"
        # If variant has no prompt file, resolve prompt relative to base agent dir
        if not config.prompt_path.exists():
            config._base_prompt_path = AGENTS_DIR / base_name / config.prompt_file

    return config
