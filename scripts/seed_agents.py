"""One-time seed: read agents from filesystem and insert into Supabase agents table.

Usage:
    uv run python scripts/seed_agents.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from dotenv import load_dotenv

load_dotenv()

from core.supabase_client import get_supabase

AGENTS_DIR = Path(__file__).parent.parent / "agents"


def read_agent(agent_name: str) -> dict:
    agent_dir = AGENTS_DIR / agent_name

    with open(agent_dir / "config.yaml") as f:
        cfg = yaml.safe_load(f)

    prompt_file = cfg.get("prompt_file", "prompt.txt")
    prompt_path = agent_dir / prompt_file
    system_prompt = prompt_path.read_text() if prompt_path.exists() else ""

    post_call_raw = cfg.get("post_call_analyses") or []
    post_call_analyses = []
    for a in post_call_raw:
        pf = a.get("prompt_file", "")
        prompt_text = ""
        if pf:
            p = agent_dir / pf
            if p.exists():
                prompt_text = p.read_text()
        post_call_analyses.append({
            "name": a["name"],
            "model": a.get("model", "claude-haiku-4-5-20251001"),
            "prompt": prompt_text,
            "output_type": a.get("output_type", "text"),
        })

    llm = cfg.get("llm", {})
    tts = cfg.get("tts", {})
    tts_settings = tts.get("settings", {})
    stt = cfg.get("stt", {})
    telephony = cfg.get("telephony", {})
    recording = cfg.get("recording", {})

    return {
        "name": cfg["name"],
        "display_name": cfg.get("display_name", cfg["name"]),
        "description": cfg.get("description", ""),
        "type": cfg.get("type", "outbound"),
        "llm_provider": llm.get("provider", "anthropic"),
        "llm_model": llm.get("model", "claude-sonnet-4-6"),
        "temperature": llm.get("temperature", 0.7),
        "max_tokens": llm.get("max_tokens", 200),
        "enable_prompt_caching": llm.get("enable_prompt_caching", True),
        "tts_provider": tts.get("provider", "elevenlabs"),
        "tts_voice_id": tts.get("voice_id", ""),
        "tts_model": tts.get("model", "eleven_turbo_v2_5"),
        "tts_stability": tts_settings.get("stability"),
        "tts_similarity_boost": tts_settings.get("similarity_boost"),
        "tts_style": tts_settings.get("style"),
        "tts_use_speaker_boost": tts_settings.get("use_speaker_boost"),
        "tts_speed": tts_settings.get("speed"),
        "stt_provider": stt.get("provider", "deepgram"),
        "phone_number": telephony.get("phone_number", ""),
        "transfer_targets": cfg.get("transfer_targets", {}),
        "tools": cfg.get("tools", ["end_call"]),
        "system_prompt": system_prompt,
        "first_message": cfg.get("first_message", ""),
        "recording_enabled": recording.get("enabled", True),
        "recording_channels": recording.get("channels", 2),
        "post_call_analyses": post_call_analyses,
        "is_active": True,
    }


def discover_agents() -> list[str]:
    agents = []
    if not AGENTS_DIR.is_dir():
        return agents
    for category in sorted(AGENTS_DIR.iterdir()):
        if not category.is_dir() or category.name.startswith(("_", ".", "__")):
            continue
        for variant in sorted(category.iterdir()):
            if variant.is_dir() and (variant / "config.yaml").exists():
                agents.append(f"{category.name}/{variant.name}")
    return agents


def main():
    sb = get_supabase()
    agent_names = discover_agents()
    if not agent_names:
        print("No agents found in", AGENTS_DIR)
        sys.exit(1)

    for name in agent_names:
        print(f"Reading {name} ...")
        row = read_agent(name)

        existing = sb.table("agents").select("id").eq("name", name).limit(1).execute()
        if existing.data:
            agent_id = existing.data[0]["id"]
            print(f"  Already exists (id={agent_id}), updating ...")
            sb.table("agents").update(row).eq("id", agent_id).execute()
        else:
            resp = sb.table("agents").insert(row).execute()
            agent_id = resp.data[0]["id"] if resp.data else "?"
            print(f"  Inserted (id={agent_id})")

    print(f"\nDone. Seeded {len(agent_names)} agent(s).")


if __name__ == "__main__":
    main()
