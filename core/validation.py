"""Shared validation for agent configuration data."""

import re
from typing import Any

E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

ALLOWED_LLM_MODELS = [
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

ALLOWED_TTS_PROVIDERS = ["elevenlabs"]
ALLOWED_TTS_MODELS: dict[str, list[str]] = {
    "elevenlabs": ["eleven_turbo_v2_5", "eleven_multilingual_v2"],
}

ALLOWED_STT_PROVIDERS = ["deepgram"]
ALLOWED_STT_LANGUAGES = ["en", "es", "fr", "de", "pt", "it", "nl", "ja", "ko", "zh"]

ALLOWED_TOOL_TYPES = ["end_call", "press_digit", "transfer_call"]

ALLOWED_VOICEMAIL_ACTIONS = ["hangup", "leave_message", "ignore"]

FIELD_LABELS: dict[str, str] = {
    "tts_stability": "Voice Temperature",
    "tts_style": "Expressiveness",
    "tts_speed": "Speed",
    "tts_use_speaker_boost": "Speaker Boost",
}

FIELD_RANGES: dict[str, dict[str, Any]] = {
    "temperature": {"min": 0.0, "max": 1.0, "step": 0.1, "default": 0.7},
    "max_tokens": {"min": 50, "max": 1000, "step": 10, "default": 390},
    "tts_stability": {"min": 0.0, "max": 1.0, "step": 0.05, "default": 0.5},
    "tts_similarity_boost": {"min": 0.0, "max": 1.0, "step": 0.05, "default": 0.8},
    "tts_style": {"min": 0.0, "max": 1.0, "step": 0.05, "default": 0.0},
    "tts_speed": {"min": 0.7, "max": 1.2, "step": 0.05, "default": 1.0},
    "idle_timeout_secs": {"min": 10, "max": 120, "step": 5, "default": 30},
    "max_call_duration_secs": {"min": 60, "max": 3600, "step": 60, "default": 1800},
    "max_retries": {"min": 0, "max": 10, "step": 1, "default": 0},
    "retry_delay_secs": {"min": 60, "max": 3600, "step": 60, "default": 300},
    "default_concurrency": {"min": 1, "max": 10, "step": 1, "default": 1},
}

TOOL_SETTINGS_SCHEMA: dict[str, dict] = {
    "end_call": {},
    "press_digit": {},
    "transfer_call": {
        "targets": {
            "type": "key_value",
            "value_format": "e164",
            "description": "Transfer destination name → phone number",
        },
    },
}

NEW_AGENT_DEFAULTS: dict[str, Any] = {
    "display_name": "",
    "description": "",
    "llm_provider": "anthropic",
    "llm_model": "claude-sonnet-4-6",
    "temperature": 0.2,
    "max_tokens": 390,
    "enable_prompt_caching": True,
    "tts_provider": "elevenlabs",
    "tts_voice_id": "",
    "tts_model": "eleven_turbo_v2_5",
    "tts_stability": 0.75,
    "tts_similarity_boost": 0.8,
    "tts_style": 0.0,
    "tts_use_speaker_boost": False,
    "tts_speed": 1.0,
    "stt_provider": "deepgram",
    "stt_language": "en",
    "stt_keywords": [],
    "tools": [
        {
            "type": "end_call",
            "description": "End the call gracefully when the conversation is complete.",
            "settings": {},
        },
    ],
    "system_prompt": "",
    "recording_enabled": True,
    "recording_channels": 2,
    "post_call_analyses": {"model": "claude-haiku-4-5-20251001", "fields": []},
    "idle_timeout_secs": 30,
    "idle_message": "Are you still there?",
    "max_call_duration_secs": 1800,
    "voicemail_action": "hangup",
    "voicemail_message": "",
    "max_retries": 0,
    "retry_delay_secs": 300,
    "default_concurrency": 1,
    "calling_window_start": "08:00",
    "calling_window_end": "17:00",
    "calling_window_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
}

_IMMUTABLE_FIELDS = {"llm_provider", "tts_provider", "stt_provider"}
_DB_ONLY_FIELDS = {"id", "created_at", "updated_at", "created_by", "is_active",
                   "extends_agent_id", "overrides"}
_STRIP_ON_UPDATE = {"name"} | _IMMUTABLE_FIELDS | _DB_ONLY_FIELDS


def _validate_range(field: str, value: Any, errors: list[str]) -> None:
    spec = FIELD_RANGES.get(field)
    if spec is None:
        return
    if not isinstance(value, (int, float)):
        errors.append(f"{field} must be a number")
        return
    if value < spec["min"] or value > spec["max"]:
        errors.append(f"{field} must be {spec['min']}–{spec['max']}")


def _validate_tools(tools: list, errors: list[str]) -> None:
    if not isinstance(tools, list):
        errors.append("tools must be an array")
        return
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            errors.append(f"tools[{i}] must be an object")
            continue
        ttype = tool.get("type")
        if ttype not in ALLOWED_TOOL_TYPES:
            errors.append(
                f"tools[{i}].type '{ttype}' invalid. "
                f"Allowed: {', '.join(ALLOWED_TOOL_TYPES)}"
            )
            continue
        if not isinstance(tool.get("description", ""), str):
            errors.append(f"tools[{i}].description must be a string")
        settings = tool.get("settings", {})
        if not isinstance(settings, dict):
            errors.append(f"tools[{i}].settings must be an object")
            continue
        if ttype == "transfer_call" and "targets" in settings:
            targets = settings["targets"]
            if not isinstance(targets, dict):
                errors.append(f"tools[{i}].settings.targets must be an object")
            else:
                for tname, number in targets.items():
                    if not E164_RE.match(str(number)):
                        errors.append(
                            f"tools[{i}].settings.targets.{tname} "
                            f"must be E.164 format (e.g. +19495551234)"
                        )


_ALLOWED_POST_CALL_FIELD_TYPES = {"text", "selector"}


def _validate_post_call_analyses(pca: Any, errors: list[str]) -> None:
    if not isinstance(pca, dict):
        errors.append("post_call_analyses must be an object with 'model' and 'fields'")
        return
    model = pca.get("model", "claude-haiku-4-5-20251001")
    if model not in ALLOWED_LLM_MODELS:
        errors.append(
            f"post_call_analyses.model '{model}' not allowed. "
            f"Options: {', '.join(ALLOWED_LLM_MODELS)}"
        )
    fields = pca.get("fields")
    if fields is None:
        return
    if not isinstance(fields, list):
        errors.append("post_call_analyses.fields must be an array")
        return
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            errors.append(f"post_call_analyses.fields[{i}] must be an object")
            continue
        name = f.get("name")
        if not name or not isinstance(name, str):
            errors.append(f"post_call_analyses.fields[{i}].name is required")
        ftype = f.get("type")
        if ftype not in _ALLOWED_POST_CALL_FIELD_TYPES:
            errors.append(
                f"post_call_analyses.fields[{i}].type '{ftype}' invalid. "
                f"Allowed: {', '.join(_ALLOWED_POST_CALL_FIELD_TYPES)}"
            )
        if ftype == "selector":
            choices = f.get("choices")
            if not choices or not isinstance(choices, list) or len(choices) == 0:
                errors.append(
                    f"post_call_analyses.fields[{i}] (selector) must have at least one choice"
                )


def validate_agent_data(
    data: dict,
    is_create: bool = False,
) -> tuple[dict, list[str]]:
    """Validate and clean agent data for create or update.

    Returns (cleaned_data, errors). Empty errors means valid.
    On create, fills defaults from NEW_AGENT_DEFAULTS for missing fields.
    """
    errors: list[str] = []
    cleaned = dict(data)

    if is_create:
        if not cleaned.get("name"):
            errors.append("name is required")
        if not cleaned.get("display_name"):
            errors.append("display_name is required")
        for key, default in NEW_AGENT_DEFAULTS.items():
            cleaned.setdefault(key, default)
    else:
        for field in _STRIP_ON_UPDATE:
            cleaned.pop(field, None)

    for field in _IMMUTABLE_FIELDS | _DB_ONLY_FIELDS:
        cleaned.pop(field, None)

    if "llm_model" in cleaned and cleaned["llm_model"] not in ALLOWED_LLM_MODELS:
        errors.append(
            f"llm_model '{cleaned['llm_model']}' not allowed. "
            f"Options: {', '.join(ALLOWED_LLM_MODELS)}"
        )

    if "tts_model" in cleaned:
        provider = cleaned.get("tts_provider", "elevenlabs")
        allowed = ALLOWED_TTS_MODELS.get(provider, [])
        if cleaned["tts_model"] not in allowed:
            errors.append(
                f"tts_model '{cleaned['tts_model']}' not allowed for {provider}. "
                f"Options: {', '.join(allowed)}"
            )

    if "stt_language" in cleaned and cleaned["stt_language"] not in ALLOWED_STT_LANGUAGES:
        errors.append(
            f"stt_language '{cleaned['stt_language']}' not allowed. "
            f"Options: {', '.join(ALLOWED_STT_LANGUAGES)}"
        )

    if "voicemail_action" in cleaned and cleaned["voicemail_action"] not in ALLOWED_VOICEMAIL_ACTIONS:
        errors.append(
            f"voicemail_action '{cleaned['voicemail_action']}' not allowed. "
            f"Options: {', '.join(ALLOWED_VOICEMAIL_ACTIONS)}"
        )

    for field in FIELD_RANGES:
        if field in cleaned:
            _validate_range(field, cleaned[field], errors)

    if "tools" in cleaned:
        _validate_tools(cleaned["tools"], errors)

    if "post_call_analyses" in cleaned:
        _validate_post_call_analyses(cleaned["post_call_analyses"], errors)

    return cleaned, errors
