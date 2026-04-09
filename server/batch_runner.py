"""Batch runner with true concurrency control, scheduling, and crash recovery.

Concurrency = max active phone calls at any time (not API requests).
The semaphore holds until a call FINISHES, not when the Twilio API returns.

Scheduling: respects calling_window_start/end/days/timezone.
Outside the window, the runner pauses and resumes automatically.
"""

from __future__ import annotations

import asyncio
import io
import time
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from openpyxl import Workbook

from core.call_result import CallResult
from core.call_store import (
    get_batch,
    get_batch_calls,
    get_call,
    increment_batch_counter,
    parse_jsonb,
    save_call_result,
    update_batch,
    update_call_fields,
)
from core.config_loader import load_agent_config
from core.supabase_client import get_supabase
from server.twilio_outbound import place_outbound_call

MAX_SERVER_CONCURRENCY = 10
WINDOW_CHECK_INTERVAL_SECS = 60
CALL_POLL_INTERVAL_SECS = 5
CALL_TIMEOUT_SECS = 1800
DIAL_RATE_LIMIT_SECS = 1.2


# ── Main entry point ────────────────────────────────────────────────────────


async def run_batch(pending_calls: dict, batch_id: str) -> None:
    """Run a batch with scheduling and concurrency control.

    Called as a background task from the start endpoint, and also on
    server startup for crash recovery of orphaned batches.
    """
    try:
        batch = await get_batch(batch_id)
        if not batch:
            logger.error(f"Batch {batch_id} not found")
            return

        agent_name = batch["agent_name"]
        from_number = batch.get("from_number", "")
        if not from_number:
            logger.error(f"Batch {batch_id} has no from_number — cannot dial")
            await update_batch(batch_id, status="failed")
            return
        tz_str = batch.get("timezone", "America/New_York")
        window_start = batch.get("calling_window_start", "09:00:00")
        window_end = batch.get("calling_window_end", "17:00:00")
        window_days = parse_jsonb(
            batch.get("calling_window_days"),
            ["Mon", "Tue", "Wed", "Thu", "Fri"],
        )
        concurrency = min(batch.get("concurrency", 5), MAX_SERVER_CONCURRENCY)

        rows_data = parse_jsonb(batch.get("rows_data"))
        rows_orig = parse_jsonb(batch.get("rows"))
        all_rows: list[dict[str, Any]] = rows_data or rows_orig
        if not isinstance(all_rows, list):
            all_rows = []

        from server.schemas import DialableRow as _DR

        validated_rows: list[dict[str, Any]] = []
        for i, raw in enumerate(all_rows):
            try:
                _DR.model_validate(raw)
                validated_rows.append(raw)
            except Exception as e:
                logger.warning(f"Batch {batch_id} row {i} invalid, skipping: {e}")
        all_rows = validated_rows

        to_dial = [r for r in all_rows if r.get("validation") == "valid"]
        if not to_dial:
            logger.error(
                f"Batch {batch_id} has no valid rows "
                f"(rows_data={len(rows_data)}, rows={len(rows_orig)}, "
                f"all_rows={len(all_rows)})"
            )
            await update_batch(batch_id, status="failed")
            return

        try:
            load_agent_config(agent_name)
        except Exception as exc:
            logger.error(f"Agent config load failed for batch {batch_id}: {exc}")
            await update_batch(batch_id, status="failed")
            return

        already_dialed = await _already_dialed_indices(batch_id)
        remaining = [
            r for r in to_dial
            if r.get("row_index") not in already_dialed
        ]
        if not remaining:
            logger.info(f"Batch {batch_id} — all rows already dialed")
            await _finalize_batch(batch_id)
            return

        total = len(to_dial)
        await update_batch(batch_id, total_rows=total, status="running")

        logger.info(
            f"Batch {batch_id}: {len(remaining)} remaining of {total} valid, "
            f"concurrency={concurrency}, window={window_start}-{window_end} {tz_str}"
        )

        sem = asyncio.Semaphore(concurrency)
        tasks = [
            asyncio.create_task(
                _dial_with_lifecycle(
                    sem=sem,
                    pending_calls=pending_calls,
                    batch_id=batch_id,
                    agent_name=agent_name,
                    from_number=from_number,
                    row=row,
                    tz_str=tz_str,
                    window_start=window_start,
                    window_end=window_end,
                    window_days=window_days,
                )
            )
            for row in remaining
        ]

        await asyncio.gather(*tasks, return_exceptions=True)
        await _finalize_batch(batch_id)

    except asyncio.CancelledError:
        logger.info(f"Batch {batch_id} was cancelled")
        await update_batch(batch_id, status="canceled")
    except Exception:
        logger.exception(f"Batch {batch_id} failed with unhandled error")
        try:
            await update_batch(batch_id, status="failed")
        except Exception as inner_e:
            logger.critical(
                f"ZOMBIE BATCH {batch_id}: failed to update status to 'failed'. "
                f"Batch stuck in 'running' — manual intervention required. "
                f"Error: {inner_e}"
            )


# ── Per-call lifecycle ───────────────────────────────────────────────────────


async def _dial_with_lifecycle(
    *,
    sem: asyncio.Semaphore,
    pending_calls: dict,
    batch_id: str,
    agent_name: str,
    from_number: str,
    row: dict,
    tz_str: str,
    window_start: str,
    window_end: str,
    window_days: list[str],
) -> None:
    """Acquire semaphore → wait for window → dial → hold until call finishes."""
    async with sem:
        batch = await get_batch(batch_id)
        if batch and batch.get("status") == "canceled":
            return

        await _wait_for_calling_window(
            batch_id, tz_str, window_start, window_end, window_days,
        )

        batch = await get_batch(batch_id)
        if batch and batch.get("status") == "canceled":
            return

        if batch and batch.get("paused_by_user"):
            while True:
                await asyncio.sleep(WINDOW_CHECK_INTERVAL_SECS)
                batch = await get_batch(batch_id)
                if not batch or not batch.get("paused_by_user"):
                    break
                if batch.get("status") == "canceled":
                    return

        await update_batch(batch_id, status="running")

        await asyncio.sleep(DIAL_RATE_LIMIT_SECS)

        row_index = int(row.get("row_index", 0))
        phone = row.get("phone_e164", "")
        if not phone:
            logger.error(
                f"Batch {batch_id} row {row_index}: empty phone_e164, skipping"
            )
            await increment_batch_counter(batch_id, "failed_rows")
            return
        case_data = row.get("case_data") or {}
        call_id = str(uuid.uuid4())

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
            _cid, _sid = await _place_call_with_retry(
                pending_calls=pending_calls,
                agent_name=agent_name,
                target_number=phone,
                case_data=case_data,
                from_number=from_number,
                call_id=call_id,
                batch_id=batch_id,
                batch_row_index=row_index,
            )

            await _wait_for_call_completion(call_id, batch_id)

        except Exception as exc:
            logger.exception(
                f"Batch {batch_id} row {row_index} error: {exc}"
            )
            pending_calls.pop(call_id, None)
            await update_call_fields(
                call_id,
                status="failed",
                error=f"Dial error: {str(exc)[:500]}",
            )
            await increment_batch_counter(batch_id, "failed_rows")


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _already_dialed_indices(batch_id: str) -> set[int]:
    calls = await get_batch_calls(batch_id)
    indices: set[int] = set()
    for c in calls:
        idx = c.get("batch_row_index")
        if idx is not None:
            indices.add(int(idx))
    return indices


async def _wait_for_calling_window(
    batch_id: str,
    tz_str: str,
    window_start: str,
    window_end: str,
    window_days: list[str],
) -> None:
    try:
        tz = ZoneInfo(tz_str)
    except Exception as e:
        logger.error(
            f"Batch {batch_id}: invalid timezone '{tz_str}', "
            f"falling back to UTC: {e}"
        )
        tz = ZoneInfo("UTC")

    while True:
        batch = await get_batch(batch_id)
        if not batch or batch.get("status") == "canceled":
            return

        now = datetime.now(tz)

        start_date_str = batch.get("start_date")
        if start_date_str:
            try:
                start_date = datetime.strptime(str(start_date_str), "%Y-%m-%d").date()
                if now.date() < start_date:
                    await update_batch(batch_id, status="scheduled")
                    logger.info(
                        f"Batch {batch_id} waiting for start date {start_date_str}. "
                        f"Today: {now.date()}"
                    )
                    await asyncio.sleep(WINDOW_CHECK_INTERVAL_SECS)
                    continue
            except ValueError:
                pass

        day_abbr = now.strftime("%a")
        current_time = now.time()

        fmt = "%H:%M:%S" if len(window_start) > 5 else "%H:%M"
        try:
            start_t = datetime.strptime(window_start, fmt).time()
            end_t = datetime.strptime(window_end, fmt).time()
        except ValueError as e:
            logger.error(
                f"Batch {batch_id}: invalid window format "
                f"'{window_start}'-'{window_end}', using 24/7: {e}"
            )
            start_t = datetime.strptime("00:00:00", "%H:%M:%S").time()
            end_t = datetime.strptime("23:59:59", "%H:%M:%S").time()

        if day_abbr in window_days and start_t <= current_time <= end_t:
            return

        await update_batch(batch_id, status="paused")
        logger.info(
            f"Batch {batch_id} outside calling window "
            f"({window_start}-{window_end} {window_days} {tz_str}). "
            f"Now: {day_abbr} {current_time}. Sleeping {WINDOW_CHECK_INTERVAL_SECS}s..."
        )
        await asyncio.sleep(WINDOW_CHECK_INTERVAL_SECS)


async def _place_call_with_retry(
    *,
    pending_calls: dict,
    agent_name: str,
    target_number: str,
    case_data: dict,
    from_number: str,
    call_id: str,
    batch_id: str,
    batch_row_index: int,
    max_retries: int = 3,
) -> tuple[str, str]:
    """Place a Twilio call with exponential backoff retry.

    Raises on final failure (caller handles logging + counter).
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            return place_outbound_call(
                pending_calls,
                agent_name=agent_name,
                target_number=target_number,
                case_data=case_data,
                from_number=from_number,
                call_id=call_id,
                batch_id=batch_id,
                batch_row_index=batch_row_index,
            )
        except Exception as exc:
            last_exc = exc
            logger.warning(
                f"Twilio dial attempt {attempt + 1}/{max_retries} "
                f"for batch {batch_id} call {call_id}: {exc}"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** (attempt + 1))

    raise last_exc  # type: ignore[misc]


async def _wait_for_call_completion(call_id: str, batch_id: str) -> None:
    """Poll the call record until it reaches a terminal status.

    THIS is what makes concurrency real — the semaphore slot stays
    occupied until the phone call actually finishes.
    """
    terminal = {"completed", "failed", "no_answer", "busy", "canceled"}
    deadline = time.monotonic() + CALL_TIMEOUT_SECS
    while time.monotonic() < deadline:
        call = await get_call(call_id)
        if call and call.get("status") in terminal:
            return
        await asyncio.sleep(CALL_POLL_INTERVAL_SECS)

    await update_call_fields(
        call_id,
        status="failed",
        error=f"Call timed out after {CALL_TIMEOUT_SECS}s",
    )
    await increment_batch_counter(batch_id, "failed_rows")


# ── Batch finalization ───────────────────────────────────────────────────────


async def _finalize_stale_batch_calls(batch_id: str) -> None:
    """Mark any still-pending batch calls as no_answer.

    Counter updates are handled by _finalize_batch reconciliation,
    so we only update call status here.
    """
    calls = await get_batch_calls(batch_id)
    for row in calls:
        if row.get("status") in ("pending", "in_progress"):
            cid = row["id"]
            try:
                await update_call_fields(
                    cid,
                    status="no_answer",
                    error="Call did not complete — stale at batch finalization",
                )
            except Exception:
                logger.exception("Failed to finalize stale call %s", cid)


async def _finalize_batch(batch_id: str) -> None:
    """Clean up after all rows are processed."""
    await _finalize_stale_batch_calls(batch_id)

    try:
        calls = await get_batch_calls(batch_id)
        actual_completed = sum(1 for c in calls if c.get("status") == "completed")
        actual_failed = sum(
            1 for c in calls
            if c.get("status") in ("failed", "no_answer", "busy", "canceled")
        )
        await update_batch(
            batch_id,
            completed_rows=actual_completed,
            failed_rows=actual_failed,
        )
        logger.info(
            f"Batch {batch_id} counters reconciled: "
            f"{actual_completed} completed, {actual_failed} failed"
        )
    except Exception as e:
        logger.warning(f"Batch {batch_id} counter reconciliation failed: {e}")

    try:
        await build_and_upload_batch_results(batch_id)
    except Exception:
        logger.exception("Failed to build results for batch %s", batch_id)
    batch = await get_batch(batch_id)
    if batch and batch.get("status") != "canceled":
        await update_batch(batch_id, status="completed")
    logger.info(f"Batch {batch_id} finalized")


# ── Results Excel builder ────────────────────────────────────────────────────


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
    """Build results xlsx from batch calls; upload to Storage; return path."""
    batch = await get_batch(batch_id)
    if not batch:
        return None
    agent_name = batch["agent_name"]
    config = load_agent_config(agent_name)
    pca = config.post_call_analyses
    if pca is None:
        analysis_names: list[str] = []
    elif isinstance(pca, list):
        analysis_names = [a.name for a in pca]
    elif hasattr(pca, "fields") and pca.fields:
        analysis_names = [f.name for f in pca.fields]
    else:
        analysis_names = []

    calls = await get_batch_calls(batch_id)
    by_row: dict[int, dict[str, Any]] = {}
    for c in calls:
        idx = c.get("batch_row_index")
        if idx is not None:
            by_row[int(idx)] = c

    rows_payload: list[dict[str, Any]] = (
        parse_jsonb(batch.get("rows_data")) or parse_jsonb(batch.get("rows"))
    )
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
        + ["phone_e164", "call_id", "call_status", "call_duration_secs", "recording_url"]
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
