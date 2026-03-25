"""Prompt hydration — replaces {{variable}} placeholders with case data values."""

import re
from datetime import datetime
from pathlib import Path

VOICE_WRAPPER = (
    "# GLOBAL VOICE SYSTEM INSTRUCTIONS\n"
    "You are a specialized voice AI connected to a live, low-latency phone call.\n"
    "- NEVER use markdown formatting (no asterisks, bolding, or bullet points).\n"
    "- ALWAYS speak in short, punchy, conversational sentences.\n"
    "- If you must provide a long string of data (like spelling an NPI or Claim number), "
    "that is the ONLY thing you should say in that turn. Do not add conversational filler "
    "to data readouts.\n"
    "---\n"
)


def hydrate_prompt(prompt_path: Path, case_data: dict) -> str:
    """Read a prompt template and replace all {{Variable_Name}} placeholders.

    - If a variable has no value, replace with empty string
    - Always injects {{current_time}} with formatted current datetime
    - Prepends the global voice system instructions wrapper
    """
    prompt = prompt_path.read_text()

    prompt = prompt.replace(
        "{{current_time}}", datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
    )

    for key, value in case_data.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", str(value) if value else "")

    prompt = re.sub(r"\{\{[^}]+\}\}", "", prompt)

    return VOICE_WRAPPER + prompt
