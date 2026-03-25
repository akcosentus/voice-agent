#!/usr/bin/env python3
"""Thin CLI for batch campaigns — talks to the FastAPI server only."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import httpx


async def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a batch file and run outbound dials via API")
    parser.add_argument("--agent", required=True, help="Agent name, e.g. chris/claim_status")
    parser.add_argument("--input", required=True, type=Path, help="Path to .xlsx or .csv")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel Twilio dials")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BATCH_API_BASE", "http://127.0.0.1:8000"),
        help="API base URL (or set BATCH_API_BASE)",
    )
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"File not found: {args.input}", file=sys.stderr)
        return 1

    base = args.base_url.rstrip("/")
    data = args.input.read_bytes()

    async with httpx.AsyncClient(timeout=120.0) as client:
        up = await client.post(
            f"{base}/api/batches/upload",
            files={"file": (args.input.name, data)},
            data={"agent_name": args.agent},
        )
        if up.status_code != 200:
            print(up.text, file=sys.stderr)
            return 1
        body = up.json()
        batch_id = body["batch_id"]
        print(f"Uploaded batch {batch_id} — summary: {body.get('summary')}")

        st = await client.post(
            f"{base}/api/batches/{batch_id}/start",
            params={"concurrency": args.concurrency},
        )
        if st.status_code != 200:
            print(st.text, file=sys.stderr)
            return 1
        print(f"Started batch: {st.json()}")

        while True:
            await asyncio.sleep(5.0)
            r = await client.get(f"{base}/api/batches/{batch_id}/status")
            if r.status_code != 200:
                print(r.text, file=sys.stderr)
                return 1
            s = r.json()
            print(
                f"  status={s.get('status')} "
                f"completed={s.get('completed')}/{s.get('total')} "
                f"failed={s.get('failed')}"
            )
            if s.get("status") in ("completed", "failed"):
                break

        if s.get("status") != "completed":
            print("Batch did not complete successfully.", file=sys.stderr)
            return 1

        res = await client.get(f"{base}/api/batches/{batch_id}/results")
        if res.status_code != 200:
            print(res.text, file=sys.stderr)
            return 1

        out_name = args.input.stem + "_results.xlsx"
        out_path = args.input.parent / out_name
        out_path.write_bytes(res.content)
        print(f"Saved results to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
