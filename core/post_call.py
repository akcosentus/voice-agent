"""Run per-agent post-call LLM analyses (Anthropic API, not Pipecat)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anthropic

from core.config_loader import AgentConfig, PostCallAnalysis


def format_transcript(transcript: list[dict[str, Any]]) -> str:
    """Turn transcript entries into readable dialogue."""
    lines: list[str] = []
    for turn in transcript:
        role = turn.get("role", "")
        content = turn.get("content", "")
        text = _stringify_content(content)
        label = "Agent" if role == "assistant" else "Rep" if role == "user" else role or "?"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", item)))
            else:
                parts.append(str(item))
        return " ".join(parts)
    return str(content)


def load_analysis_prompt(config: AgentConfig, prompt_file: str) -> str:
    path = Path(config.prompt_path).parent / prompt_file
    if not path.is_file():
        path = config.agent_dir / prompt_file
    if not path.is_file():
        raise FileNotFoundError(f"Post-call prompt not found: {prompt_file}")
    return path.read_text(encoding="utf-8")


def hydrate_analysis_prompt(prompt: str, transcript_text: str, case_data: dict[str, Any]) -> str:
    out = prompt.replace("{{transcript}}", transcript_text)
    for key, value in case_data.items():
        out = out.replace(f"{{{{{key}}}}}", str(value) if value is not None else "")
    out = re.sub(r"\{\{[^}]+\}\}", "", out)
    return out


def parse_result(text: str, output_type: str) -> Any:
    t = text.strip()
    if output_type == "boolean":
        return t.lower() in ("true", "yes", "1")
    if output_type == "json":
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            return {"_parse_error": True, "raw": t}
    return t


async def call_anthropic(model: str, prompt: str, max_tokens: int) -> str:
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    block = response.content[0]
    if hasattr(block, "text"):
        return block.text
    return str(block)


async def run_post_call_analyses(
    config: AgentConfig,
    transcript: list[dict[str, Any]],
    case_data: dict[str, Any],
) -> dict[str, Any]:
    if not config.post_call_analyses:
        return {}
    transcript_text = format_transcript(transcript)
    results: dict[str, Any] = {}
    for analysis in config.post_call_analyses:
        results[analysis.name] = await run_single_analysis(
            config, analysis, transcript_text, case_data
        )
    return results


async def run_single_analysis(
    config: AgentConfig,
    analysis: PostCallAnalysis,
    transcript_text: str,
    case_data: dict[str, Any],
) -> Any:
    raw = load_analysis_prompt(config, analysis.prompt_file)
    prompt = hydrate_analysis_prompt(raw, transcript_text, case_data)
    text = await call_anthropic(
        model=analysis.model,
        prompt=prompt,
        max_tokens=1024,
    )
    return parse_result(text, analysis.output_type)
