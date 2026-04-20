"""Post-call analysis — batched structured extraction via a single LLM call."""

from __future__ import annotations

import json
from typing import Any

import anthropic
from loguru import logger

from core.config_loader import AgentConfig


def _format_transcript(transcript: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for turn in transcript:
        role = turn.get("role", "")
        content = turn.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                item if isinstance(item, str) else str(item.get("text", item))
                for item in content
            )
        label = "Agent" if role == "assistant" else "Caller" if role == "user" else role or "?"
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


def _build_field_instructions(fields: list) -> str:
    parts: list[str] = []
    for i, field in enumerate(fields, 1):
        if field.type == "selector":
            choices_str = ", ".join(field.choices)
            line = f'{i}. {field.name} (select ONE of: {choices_str}): {field.description}'
        else:
            line = f'{i}. {field.name} (text): {field.description}'
            if field.format_examples:
                line += f'\n   Example format: "{field.format_examples[0]}"'
        parts.append(line)
    return "\n".join(parts)


async def run_post_call_analyses(
    config: AgentConfig,
    transcript: list[dict[str, Any]],
    case_data: dict[str, Any],
) -> dict[str, Any]:
    """Run all post-call analysis fields in a single batched LLM call."""
    pca = config.post_call_analyses
    if not pca or not pca.fields:
        return {}

    if not transcript:
        logger.info("[POST-CALL] Skipping analysis — empty transcript")
        return {}

    transcript_text = _format_transcript(transcript)
    field_instructions = _build_field_instructions(pca.fields)

    case_block = json.dumps(case_data, indent=2) if case_data else "No case data provided."

    prompt = (
        "You are analyzing a completed phone call. "
        "Read the transcript and extract the following fields.\n\n"
        f"Transcript:\n{transcript_text}\n\n"
        f"Case data:\n{case_block}\n\n"
        f"Fields to extract:\n\n{field_instructions}\n\n"
        "Respond ONLY with a valid JSON object mapping each field name to its value. "
        "For selector fields, use exactly one of the provided choices. "
        "For text fields, write a concise response.\n\n"
        "Output JSON only — no markdown, no backticks, no explanation."
    )

    client = anthropic.AsyncAnthropic()

    _FALLBACK_MODEL = "claude-sonnet-4-6"
    model = pca.model
    for attempt_model in [model, _FALLBACK_MODEL]:
        try:
            response = await client.messages.create(
                model=attempt_model,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )

            result_text = response.content[0].text.strip()
            result_text = result_text.replace("```json", "").replace("```", "").strip()

            results = json.loads(result_text)

            for field in pca.fields:
                if field.type == "selector" and field.name in results:
                    if results[field.name] not in field.choices:
                        results[field.name] = f"invalid: {results[field.name]}"

            if attempt_model != model:
                logger.warning(f"[POST-CALL] Used fallback model {attempt_model} (primary {model} unavailable)")
            logger.info(f"[POST-CALL] Analysis complete: {list(results.keys())}")
            return results

        except anthropic.NotFoundError:
            if attempt_model != _FALLBACK_MODEL:
                logger.warning(f"[POST-CALL] Model {attempt_model} not found, trying {_FALLBACK_MODEL}")
                continue
            logger.error(f"[POST-CALL] Fallback model {_FALLBACK_MODEL} also not found")
            return {"_error": f"No available model (tried {model}, {_FALLBACK_MODEL})"}
        except json.JSONDecodeError as e:
            logger.error(f"[POST-CALL] Failed to parse JSON response: {e}")
            return {"_error": "Failed to parse analysis response"}
        except Exception as e:
            logger.error(f"[POST-CALL] Analysis failed: {e}")
            return {"_error": str(e)}

    return {"_error": "Post-call analysis exhausted all model options"}
