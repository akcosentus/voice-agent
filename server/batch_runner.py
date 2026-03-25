"""Batch campaign: schedule outbound dials, watch completion, build results Excel."""

from __future__ import annotations

import asyncio
import io
import time
import uuid
from typing import Any

from loguru import logger
from openpyxl import Workbook

from core.call_result import CallResult
from core.call_store import (
    get_batch,
    get_batch_calls,
    save_call_result,
    update_batch,
    update_call_fields,
    increment_batch_counter,
)
from core.config_loader import load_agent_config
from core.supabase_client import get_supabase
from server.twilio_outbound import place_outbound_call


async def _finalize_stale_batch_calls(batch_id: str) -> None:
    """Mark any still-pending batch calls as no_answer and bump failed_rows."""
    calls = await get_batch_calls(batch_id)
    for row in calls:
        if row.get("status") == "pending":
            cid = row["id"]
            try:
                await update_call_fields(
                    cid,
                    status="no_answer",
                    error="Call did not connect or timed out before WebSocket opened",
                )
                await increment_batch_counter(batch_id, "failed_rows")
            except Exception:
                logger.exception("Failed to finalize stale call %s", cid)


def _signed_recording_url(storage_path: str | None, expires_in: int = 3600) -> str:
    if not storage_path:
        return ""
    try:
        supabase = get_supabase()
        r = supabase.storage.from_("recordings").create_signed_url(
            storage_path, expires_in
        )
        if isinstance(r, dict):
            return r.get("signedURL") or r.get("signedUrl") or ""
        return str(getattr(r, "signed_url", "") or "")
    except Exception:
        logger.exception("Signed URL failed for %s", storage_path)
        return ""


async def build_and_upload_batch_results(batch_id: str) -> str | None:
    """Build results xlsx from batch calls; upload to Storage; return storage path."""
    batch = await get_batch(batch_id)
    if not batch:
        return None
    agent_name = batch["agent_name"]
    config = load_agent_config(agent_name)
    analysis_names = [a.name for a in config.post_call_analyses]

    calls = await get_batch_calls(batch_id)
    by_row: dict[int, dict[str, Any]] = {}
    for c in calls:
        idx = c.get("batch_row_index")
        if idx is not None:
            by_row[int(idx)] = c

    rows_payload: list[dict[str, Any]] = batch.get("rows") or []
    rows_payload = sorted(rows_payload, key=lambda r: r.get("row_index", 0))

    wb = Workbook()
    ws = wb.active
    if ws is None:
        return None
    ws.title = "results"

    base_keys: list[str] = []
    if rows_payload:
        cd = rows_payload[0].get("case_data") or {}
        base_keys = list(cd.keys())

    headers = (
        list(base_keys)
        + [
            "phone_e164",
            "call_id",
            "call_status",
            "call_duration_secs",
            "recording_url",
        ]
        + analysis_names
    )
    ws.append(headers)

    for pr in rows_payload:
        idx = int(pr.get("row_index", 0))
        case_data = pr.get("case_data") or {}
        phone = pr.get("phone_e164", "")
        if pr.get("validation") != "valid":
            row_out = [case_data.get(k, "") for k in base_keys]
            row_out += [phone, "", "not_dialed", "", ""] + [""] * len(analysis_names)
            ws.append(row_out)
            continue

        call = by_row.get(idx, {})
        post = call.get("post_call_analyses") or {}
        if isinstance(post, str):
            post = {}

        rec_path = call.get("recording_path")
        rec_url = _signed_recording_url(rec_path) if rec_path else ""

        row_out = [case_data.get(k, "") for k in base_keys]
        row_out += [
            phone,
            call.get("id", ""),
            call.get("status", ""),
            call.get("duration_secs", ""),
            rec_url,
        ]
        for an in analysis_names:
            row_out.append(post.get(an, ""))
        ws.append(row_out)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    data = buf.read()
    out_path = f"batch-files/{batch_id}/results.xlsx"
    get_supabase().storage.from_("batch-files").upload(
        out_path,
        data,
        file_options={
            "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "upsert": "true",
        },
    )
    await update_batch(batch_id, output_file_path=out_path)
    return out_path


async def watch_batch_until_done(batch_id: str, total_rows: int) -> None:
    """Wait until all rows are accounted for, then write results."""
    deadline = time.monotonic() + 7200.0
    try:
        while time.monotonic() < deadline:
            batch = await get_batch(batch_id)
            if not batch:
                return
            done = (batch.get("completed_rows") or 0) + (batch.get("failed_rows") or 0)
            if total_rows > 0 and done >= total_rows:
                break
            await asyncio.sleep(10.0)
        await _finalize_stale_batch_calls(batch_id)
        await build_and_upload_batch_results(batch_id)
        await update_batch(batch_id, status="completed")
    except Exception:
        logger.exception("Batch watcher failed for %s", batch_id)
        try:
            await update_batch(batch_id, status="failed")
        except Exception:
            pass


async def run_batch_dial(
    pending_calls: dict,
    batch_id: str,
    concurrency: int,
) -> None:
    """Create pending call rows, dial Twilio with a semaphore, then watch completion."""
    batch = await get_batch(batch_id)
    if not batch:
        logger.error("Batch not found: %s", batch_id)
        return
    agent_name = batch["agent_name"]
    config = load_agent_config(agent_name)
    from_number = batch.get("from_number") or config.telephony.phone_number
    rows_payload: list[dict[str, Any]] = batch.get("rows") or []
    to_dial = [r for r in rows_payload if r.get("validation") == "valid"]
    total = len(to_dial)
    if total == 0:
        await update_batch(batch_id, status="failed")
        return

    await update_batch(batch_id, status="running", total_rows=total)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def dial_one(row: dict[str, Any]) -> None:
        async with sem:
            call_id = str(uuid.uuid4())
            case_data = row.get("case_data") or {}
            phone = row["phone_e164"]
            row_index = int(row.get("row_index", 0))
            cr = CallResult(
                call_id=call_id,
                agent_name=agent_name,
                target_number=phone,
                direction="outbound",
                status="pending",
                case_data=case_data,
                batch_id=batch_id,
                batch_row_index=row_index,
            )
            await save_call_result(cr)
            try:
                place_outbound_call(
                    pending_calls,
                    agent_name=agent_name,
                    target_number=phone,
                    case_data=case_data,
                    from_number=from_number,
                    call_id=call_id,
                    batch_id=batch_id,
                    batch_row_index=row_index,
                )
            except Exception:
                logger.exception("Twilio dial failed for batch %s row %s", batch_id, row_index)
                await update_call_fields(
                    call_id,
                    status="failed",
                    error="Twilio outbound failed",
                )
                await increment_batch_counter(batch_id, "failed_rows")

    await asyncio.gather(*[dial_one(r) for r in to_dial])
    asyncio.create_task(watch_batch_until_done(batch_id, total))
