"""Centralized loguru configuration.

This module must be imported exactly once, as early as possible in the
process lifecycle (before any module that emits logs does its first
emit). ``server/main.py`` imports it at the top of the file.

Two responsibilities:

1. Replace loguru's default stderr sink with our own that routes every
   record through :func:`_phi_filter`.
2. Keep verbose DEBUG across our own modules (they're audited and clean)
   while dropping specific Pipecat vendor log messages that dump full
   prompts / conversation history to the journal.

Adding a new LLM or image vendor? Audit its ``logger.debug`` calls for
prompt/system-instruction/messages dumps and add the literal message
fingerprint to :data:`_PROMPT_LEAK_FINGERPRINTS` below.
"""

from __future__ import annotations

import os
import sys

from loguru import logger

# Pipecat vendor code emits these message fingerprints at DEBUG. Each one
# interpolates a full prompt, system instruction, or conversation history
# which — for us — is hydrated with case_data (PHI). Drop the record.
#
# Audit date: 2026-03-25 against pipecat-ai 0.0.106 / 0.0.108.
# Re-audit command on version bumps:
#   grep -rnE 'logger\.debug.*(system_instruction|Generating chat|Generating image|Creating response)' \
#     .venv/lib/python3.11/site-packages/pipecat/services/
_PROMPT_LEAK_FINGERPRINTS: tuple[str, ...] = (
    ": Using system instruction: ",     # anthropic.llm:317, openai.base_llm:207,
                                        # aws.llm:895, aws.nova_sonic.llm:651
    ": Generating chat from ",          # anthropic.llm:512 (prod-firing),
                                        # aws.llm:1194, google.llm:1059/1077
    "Generating image from prompt: ",   # openai.image:117, google.image:151,
                                        # fal.image:185
    "Creating response: ",              # openai_realtime_beta.openai:815
)


def _phi_filter(record: dict) -> bool:
    """Return ``False`` to suppress a record that is known to leak PHI.

    Match rule: Pipecat namespace + DEBUG level + any fingerprint
    substring present anywhere in the formatted message.

    Our own code (``core.*``, ``server.*``) always passes through; any
    reintroduced leak there should be fixed at the emit site, not here.
    """
    if record["name"].startswith("pipecat.") and record["level"].name == "DEBUG":
        msg = record["message"]
        for fingerprint in _PROMPT_LEAK_FINGERPRINTS:
            if fingerprint in msg:
                return False
    return True


def configure_logging() -> None:
    """Wipe loguru's default sink and install our filtered stderr sink.

    Respects ``LOG_LEVEL`` env var (default ``DEBUG``). In ECS we'll set
    ``LOG_LEVEL=INFO`` for production and keep DEBUG for local/staging.

    Idempotent: safe to call more than once.
    """
    logger.remove()
    level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logger.add(
        sys.stderr,
        level=level,
        filter=_phi_filter,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )


configure_logging()
