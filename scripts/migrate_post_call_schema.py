"""One-time migration: convert post_call_analyses from old array format to new object format."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(__file__).parent.parent / ".env")

from core.supabase_client import get_supabase  # noqa: E402

NEW_PCA = {
    "model": "claude-haiku-4-5-20251001",
    "fields": [
        {
            "name": "call_summary",
            "type": "text",
            "description": (
                "Concise summary covering: 1) Claim outcome or status discussed. "
                "2) Key facts obtained (reference numbers, denial reasons, dates, amounts). "
                "3) Follow-up actions the team must take."
            ),
            "format_examples": [
                "Claim CLM-456 denied for timely filing on 1/15/2026. "
                "Appeal deadline 90 days from denial. Submit appeal via fax to "
                "949-436-0836. Follow-up: file appeal before 4/15/2026."
            ],
        },
    ],
}


def migrate():
    sb = get_supabase()
    agent_name = "chris/claim_status"

    for table in ("agents", "agent_drafts"):
        resp = sb.table(table).select("name, post_call_analyses").eq("name", agent_name).execute()
        if not resp.data:
            print(f"  [{table}] No row for {agent_name} — skipping")
            continue

        old = resp.data[0].get("post_call_analyses")
        print(f"  [{table}] Old value: {old}")

        sb.table(table).update({"post_call_analyses": NEW_PCA}).eq("name", agent_name).execute()
        print(f"  [{table}] Updated to new schema")


if __name__ == "__main__":
    migrate()
    print("Done.")
