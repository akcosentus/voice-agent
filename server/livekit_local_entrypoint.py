"""Local dev entrypoint to run a voice agent as a LiveKit bot.

Usage::

    python -m server.livekit_local_entrypoint --agent=chris-claim-status --room=test-1

Reads ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` / ``LIVEKIT_API_SECRET`` from
env. Defaults are aligned with ``livekit/livekit-server --dev`` running
on ``ws://localhost:7880`` (see ``docs/livekit_local_dev.md``).

On startup, prints a copy-pasteable ``meet.livekit.io/custom`` join URL
pre-populated with a caller JWT so you can join the same room from a
browser as the human side of the conversation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import timedelta
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(override=True)

# Must come before any module that emits logs: installs the PHI-filtered
# loguru sink from Task 3.
import core.logging  # noqa: E402, F401

from livekit import api as lkapi  # noqa: E402
from loguru import logger  # noqa: E402

from server.livekit_handler import run_livekit_agent  # noqa: E402


# Defaults matching `docker run --rm ... livekit/livekit-server --dev`
# from docs/livekit_local_dev.md.
_DEFAULT_URL = "ws://localhost:7880"
_DEFAULT_API_KEY = "devkey"
_DEFAULT_API_SECRET = "secret"


def _generate_token(
    api_key: str,
    api_secret: str,
    identity: str,
    room_name: str,
    display_name: str | None = None,
    ttl_minutes: int = 60,
) -> str:
    """Generate a LiveKit JWT authorising ``identity`` to join
    ``room_name`` with publish + subscribe grants for the given TTL."""
    return (
        lkapi.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(display_name or identity)
        .with_grants(
            lkapi.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_ttl(timedelta(minutes=ttl_minutes))
        .to_jwt()
    )


def _build_meet_join_url(url: str, token: str) -> str:
    """Build a meet.livekit.io/custom URL that auto-joins with ``token``."""
    return (
        "https://meet.livekit.io/custom"
        f"?liveKitUrl={quote(url, safe='')}"
        f"&token={quote(token, safe='')}"
    )


def _parse_case_data(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"--case-data is not valid JSON: {e}")
        sys.exit(2)
    if not isinstance(parsed, dict):
        logger.error(f"--case-data must be a JSON object, got {type(parsed).__name__}")
        sys.exit(2)
    return parsed


def _resolve(
    env_name: str, cli_value: str | None, hardcoded_default: str
) -> tuple[str, str]:
    """Standard CLI precedence: explicit CLI > env var > hardcoded default.

    CLI flags default to ``None`` in :func:`_build_parser` so we can tell
    "user typed nothing" from "user typed the default". Returns
    ``(value, source)`` so the startup banner can show where each value
    came from — important because ``.env`` may contain LiveKit Cloud
    credentials that would otherwise silently hijack the local run.
    """
    if cli_value is not None:
        return cli_value, "cli"
    env_value = os.getenv(env_name)
    if env_value:
        return env_value, f"env:{env_name}"
    return hardcoded_default, "default"


async def _async_main(args: argparse.Namespace) -> None:
    url, url_src = _resolve("LIVEKIT_URL", args.url, _DEFAULT_URL)
    api_key, key_src = _resolve("LIVEKIT_API_KEY", args.api_key, _DEFAULT_API_KEY)
    api_secret, sec_src = _resolve("LIVEKIT_API_SECRET", args.api_secret, _DEFAULT_API_SECRET)

    if not url or not api_key or not api_secret:
        logger.error(
            "Missing LiveKit credentials. Set LIVEKIT_URL / LIVEKIT_API_KEY "
            "/ LIVEKIT_API_SECRET env vars, or pass --url / --api-key / "
            "--api-secret. Defaults target livekit-server --dev on localhost."
        )
        sys.exit(1)

    case_data = _parse_case_data(args.case_data)

    bot_token = _generate_token(
        api_key,
        api_secret,
        identity="agent-bot",
        room_name=args.room,
        display_name=args.agent,
    )
    caller_token = _generate_token(
        api_key,
        api_secret,
        identity="caller",
        room_name=args.room,
        display_name="Human Caller",
    )
    caller_join_url = _build_meet_join_url(url, caller_token)

    # Print a one-shot banner to stdout so it's easy to find amid loguru
    # output. The caller URL is the piece the human operator needs.
    banner = [
        "",
        "=" * 78,
        "LIVEKIT LOCAL DEV — VOICE AGENT BOT",
        "=" * 78,
        f"  Agent:     {args.agent}",
        f"  Room:      {args.room}",
        f"  LiveKit:   {url}  [{url_src}]",
        f"  API key:   ***{api_key[-4:] if len(api_key) >= 4 else '***'}  [{key_src}]",
        f"  Secret:    ***{api_secret[-4:] if len(api_secret) >= 4 else '***'}  [{sec_src}]",
        f"  Case keys: {sorted(case_data.keys()) if case_data else '[]'}",
        "",
        "  Paste this URL into a browser to join the room as the caller:",
        "",
        f"    {caller_join_url}",
        "",
        "  Allow microphone access when the browser prompts. The bot is",
        "  already in the room; on_first_participant_joined fires the",
        "  moment you connect.",
        "=" * 78,
        "",
    ]
    # flush=True because our loguru stderr sink can otherwise drown the
    # banner and the caller URL is the one piece the human needs to see.
    print("\n".join(banner), flush=True)

    await run_livekit_agent(
        url=url,
        token=bot_token,
        room_name=args.room,
        agent_name=args.agent,
        case_data=case_data,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m server.livekit_local_entrypoint",
        description=(
            "Run a voice agent as a LiveKit bot against a local or remote "
            "LiveKit server. Defaults target `livekit/livekit-server --dev` "
            "on localhost; see docs/livekit_local_dev.md for the full runbook."
        ),
    )
    p.add_argument(
        "--agent",
        default="chris-claim-status",
        help=(
            "Agent name in Aurora. Canonical form is hyphenated "
            "(chris-claim-status) — the slash form (chris/claim_status) "
            "was pre-migration YAML naming and is no longer valid."
        ),
    )
    p.add_argument(
        "--room",
        default="test-1",
        help="LiveKit room name. Default: test-1",
    )
    p.add_argument(
        "--url",
        default=None,
        help=(
            f"LiveKit server URL. Precedence: --url > $LIVEKIT_URL > "
            f"{_DEFAULT_URL} (matches livekit-server --dev)."
        ),
    )
    p.add_argument(
        "--api-key",
        default=None,
        help=(
            "LiveKit API key. Precedence: --api-key > $LIVEKIT_API_KEY > "
            f"'{_DEFAULT_API_KEY}' (--dev server)."
        ),
    )
    p.add_argument(
        "--api-secret",
        default=None,
        help=(
            "LiveKit API secret. Precedence: --api-secret > "
            f"$LIVEKIT_API_SECRET > '{_DEFAULT_API_SECRET}' (--dev server)."
        ),
    )
    p.add_argument(
        "--case-data",
        default=None,
        help=(
            'JSON dict of hydration variables for the agent prompt, '
            'e.g. \'{"NPI":"1831323708","Patient_Name":"Alex"}\'. '
            "Defaults to empty."
        ),
    )
    return p


def main() -> None:
    args = _build_parser().parse_args()
    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
