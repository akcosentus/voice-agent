"""Unified call queue consumer.

Reads from one SQS queue. Routes messages by `source` field:
- `"batch"` → updates voice_batch_rows, triggers batch completion check.
- `"call_request"` → updates voice_call_requests (MedCloud queue).

Global semaphore limits total concurrent calls across both sources. The
consumer HOLDS its slot until the call reaches a terminal status (polled
from the respective row/request), so the pipeline's post-call writes are
visible before we release.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from loguru import logger
from openpyxl import Workbook

from core.call_store import parse_jsonb
from core.config_loader import load_agent_config
from core.lambda_client import invoke as lambda_invoke

SQS_QUEUE_URL = os.getenv(
    "CALL_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/825269749545/medcloud-voice-call-requests",
)
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "medcloud-voice-us-prod-825")

VISIBILITY_TIMEOUT_SECS = 5400  # 90 minutes — covers longest plausible call
WAIT_FOR_TERMINAL_TIMEOUT_SECS = 1800  # 30 minutes max call
TERMINAL_POLL_INTERVAL_SECS = 5
OUTSIDE_WINDOW_VISIBILITY_SECS = 300
PAUSED_VISIBILITY_SECS = 60
BATCH_CONCURRENCY_DEFER_SECS = 10
CONFIG_REFRESH_SECS = 300

_sqs = boto3.client("sqs", region_name=AWS_REGION)

_ROW_TERMINAL = {"completed", "failed", "no_answer", "busy", "skipped", "cancelled"}
_CALL_REQUEST_TERMINAL = {"completed", "failed", "no_answer", "busy", "dead_letter", "skipped"}

# Twilio errors that will never succeed on retry. See
# https://www.twilio.com/docs/api/errors — 211xx are client-side number/auth
# errors that won't resolve by waiting.
_PERMANENT_TWILIO_CODES = (
    "21211",  # Invalid 'To' phone number
    "21214",  # 'To' phone number cannot be reached
    "21215",  # Geo-permissions blocked
    "21217",  # Invalid 'To' phone number (inactive/disconnected)
    "21219",  # 'To' phone number not verified (trial account)
    "21606",  # 'From' number not a valid Twilio number
    "21608",  # 'From' number SMS-only
    "21610",  # Unsubscribed recipient
    "21612",  # Cannot route to number
    "21614",  # 'To' number not mobile
)
_PERMANENT_MARKERS = (
    "unverified",
    "may only make calls to verified",
    "not a valid phone number",
    "invalid 'to' phone number",
    "invalid 'from' phone number",
    "geo-permission",
)


def _is_permanent_failure(error_msg: str) -> bool:
    """Classify a Twilio error message as permanent (skip retries)."""
    if not error_msg:
        return False
    lower = error_msg.lower()
    if any(code in error_msg for code in _PERMANENT_TWILIO_CODES):
        return True
    return any(m in lower for m in _PERMANENT_MARKERS)


class UnifiedCallConsumer:
    """Single consumer for batch rows and call requests."""

    def __init__(self, pending_calls: dict[str, dict]):
        self._running = False
        self._pending_calls = pending_calls
        self._sem: asyncio.Semaphore | None = None
        self._global_max = 5
        self._active_calls = 0
        self._last_config_refresh: float = 0

    @property
    def status(self) -> dict[str, Any]:
        available = self._sem._value if self._sem else 0  # type: ignore[attr-defined]
        return {
            "running": self._running,
            "active_calls": self._active_calls,
            "global_max": self._global_max,
            "available_slots": available,
        }

    async def start(self) -> None:
        self._running = True
        await self._refresh_config()
        logger.info(
            f"Unified call consumer started "
            f"(global_max={self._global_max}, queue={SQS_QUEUE_URL})"
        )
        while self._running:
            try:
                if (asyncio.get_event_loop().time() - self._last_config_refresh) > CONFIG_REFRESH_SECS:
                    await self._refresh_config()
                await self._poll_once()
            except Exception:
                logger.exception("Consumer loop error")
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
        logger.info("Unified consumer stopping")

    # ── Config ──────────────────────────────────────────────────────────────

    async def _refresh_config(self) -> None:
        try:
            cfg = await lambda_invoke("GET", "/system-config/concurrency")
            if isinstance(cfg, dict):
                val = cfg.get("value") or {}
                if isinstance(val, dict):
                    self._global_max = int(val.get("global_max", 5))
        except Exception:
            logger.warning("Consumer: system-config fetch failed, keeping existing limits")

        if not self._sem:
            self._sem = asyncio.Semaphore(self._global_max)
        self._last_config_refresh = asyncio.get_event_loop().time()

    # ── Poll ────────────────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        if not self._sem:
            await asyncio.sleep(1)
            return

        free = self._sem._value  # type: ignore[attr-defined]
        if free <= 0:
            await asyncio.sleep(2)
            return

        max_msgs = min(free, 10)
        try:
            response = await asyncio.to_thread(
                _sqs.receive_message,
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=max_msgs,
                WaitTimeSeconds=5,
                VisibilityTimeout=VISIBILITY_TIMEOUT_SECS,
            )
        except Exception as exc:
            logger.warning(f"SQS receive failed: {exc}")
            await asyncio.sleep(5)
            return

        for msg in response.get("Messages", []):
            if not self._running:
                break
            asyncio.create_task(self._process_with_gate(msg))

    async def _process_with_gate(self, msg: dict[str, Any]) -> None:
        assert self._sem is not None
        async with self._sem:
            self._active_calls += 1
            try:
                await self._process_message(msg)
            finally:
                self._active_calls -= 1

    # ── Process one message ─────────────────────────────────────────────────

    async def _process_message(self, msg: dict[str, Any]) -> None:
        receipt = msg["ReceiptHandle"]
        try:
            body = json.loads(msg["Body"])
        except Exception:
            logger.exception("Bad SQS body — deleting")
            await self._delete(receipt)
            return

        source = body.get("source") or "call_request"

        # Calling window
        if not _is_within_window(body):
            await self._change_visibility(receipt, OUTSIDE_WINDOW_VISIBILITY_SECS)
            return

        # Batch pause / cancel
        if source == "batch":
            batch_id = body.get("batch_id")
            if batch_id:
                batch_data = await lambda_invoke("GET", f"/batches/{batch_id}")
                batch = (batch_data.get("batch") if isinstance(batch_data, dict) else None) or batch_data or {}
                if batch.get("status") == "cancelled":
                    row_id = body.get("batch_row_id")
                    if row_id:
                        try:
                            await lambda_invoke("PUT", f"/batch-rows/{row_id}", body={"status": "cancelled"})
                        except Exception:
                            pass
                    await self._delete(receipt)
                    return
                if batch.get("paused_by_user"):
                    await self._change_visibility(receipt, PAUSED_VISIBILITY_SECS)
                    return

        # Dedup / already processed — and pull authoritative attempt_count
        # from Aurora (SQS message body is immutable, so retries would loop).
        prior_attempts = 0
        if source == "batch" and body.get("batch_row_id"):
            row = await lambda_invoke("GET", f"/batch-rows/{body['batch_row_id']}")
            if isinstance(row, dict):
                if row.get("status") in _ROW_TERMINAL:
                    await self._delete(receipt)
                    # Row may have been terminated out-of-band (force-failed,
                    # Twilio callback, etc.) — trigger completion check so the
                    # batch doesn't hang.
                    if body.get("batch_id"):
                        await self._check_batch_completion(body["batch_id"])
                    return
                prior_attempts = int(row.get("attempt_count") or 0)
        elif source == "call_request" and body.get("request_id"):
            req = await lambda_invoke("GET", f"/call-requests/{body['request_id']}")
            if isinstance(req, dict):
                if req.get("status") in _CALL_REQUEST_TERMINAL:
                    await self._delete(receipt)
                    return
                prior_attempts = int(req.get("attempt_count") or 0)
        body["_prior_attempts"] = prior_attempts

        # Per-batch concurrency gate — throttle so only N rows from the same
        # batch are dialing at once (global semaphore is system-wide; batch
        # concurrency is a narrower per-tenant cap).
        if source == "batch" and body.get("batch_id"):
            batch_concurrency = int(
                body.get("concurrency")
                or body.get("batch_concurrency")
                or 0
            )
            if batch_concurrency > 0:
                try:
                    progress = await lambda_invoke(
                        "GET", "/batch-rows/progress",
                        query={"batch_id": body["batch_id"]},
                    )
                except Exception:
                    progress = None
                if isinstance(progress, dict):
                    currently_dialing = int(progress.get("dialing") or 0)
                    if currently_dialing >= batch_concurrency:
                        logger.debug(
                            f"Batch {body['batch_id']} at concurrency "
                            f"({currently_dialing}/{batch_concurrency}) — "
                            f"deferring row {body.get('batch_row_id')}"
                        )
                        await self._change_visibility(
                            receipt, BATCH_CONCURRENCY_DEFER_SECS
                        )
                        return

        # Pre-dial agent validation
        try:
            from server.main import validate_agent_ready
            errors = await validate_agent_ready(body.get("agent_name", ""))
        except Exception as exc:
            errors = [f"agent validation crashed: {exc}"]
        if errors:
            err_msg = "Agent not ready: " + "; ".join(errors)
            logger.error(f"Pre-dial validation failed: {err_msg}")
            await self._mark_failed(body, err_msg)
            await self._delete(receipt)
            if source == "batch" and body.get("batch_id"):
                await self._check_batch_completion(body["batch_id"])
            return

        # Dial
        try:
            await self._mark_dialing(body)
            call_id = await self._dial(body)
            logger.info(
                f"Consumer dialed {body.get('to_number')} call_id={call_id} source={source}"
            )

            # Wait for the pipeline to write the terminal status
            terminal_status = await self._wait_for_terminal(body)
            logger.info(
                f"Consumer: {body.get('to_number')} reached terminal status={terminal_status}"
            )

            # Call was handed off successfully — regardless of terminal outcome,
            # the row/request has been updated by the pipeline. Delete the message.
            await self._delete(receipt)

            if source == "batch" and body.get("batch_id"):
                await self._check_batch_completion(body["batch_id"])

        except Exception as exc:
            error_msg = str(exc)[:500]
            attempt = int(body.get("_prior_attempts") or 0) + 1
            max_attempts = int(body.get("max_attempts") or 3)
            permanent = _is_permanent_failure(error_msg)
            if permanent or attempt >= max_attempts:
                reason = "permanent failure" if permanent else "max attempts reached"
                logger.error(
                    f"Consumer: dial failed (final, {reason}) "
                    f"{body.get('to_number')}: {error_msg}"
                )
                await self._mark_failed(body, f"{reason}: {error_msg}")
                await self._delete(receipt)
                if source == "batch" and body.get("batch_id"):
                    await self._check_batch_completion(body["batch_id"])
            else:
                logger.warning(
                    f"Consumer: dial failed (attempt {attempt}/{max_attempts}) "
                    f"{body.get('to_number')}: {error_msg}"
                )
                await self._mark_retry(body, attempt, error_msg)
                # Shorten visibility timeout so SQS redelivers in ~60s
                # (we'd otherwise wait the full 90-min default)
                await self._change_visibility(receipt, 60)

    async def _dial(self, body: dict[str, Any]) -> str:
        """Place the Twilio call. Returns our internal call_id."""
        from server.twilio_outbound import place_outbound_call

        call_id, _sid = await asyncio.to_thread(
            place_outbound_call,
            self._pending_calls,
            agent_name=body["agent_name"],
            target_number=body["to_number"],
            from_number=body["from_number"],
            case_data=body.get("case_data") or {},
            batch_id=body.get("batch_id"),
            batch_row_index=body.get("row_index"),
            batch_row_id=body.get("batch_row_id"),
            request_id=body.get("request_id"),
        )
        return call_id

    async def _wait_for_terminal(self, body: dict[str, Any]) -> str:
        """Poll the row/request until the pipeline writes a terminal status.

        Holds the global semaphore the entire time so concurrency reflects
        real active calls, not just dial rate.
        """
        source = body.get("source") or "call_request"
        deadline = asyncio.get_event_loop().time() + WAIT_FOR_TERMINAL_TIMEOUT_SECS

        while asyncio.get_event_loop().time() < deadline:
            if source == "batch" and body.get("batch_row_id"):
                row = await lambda_invoke("GET", f"/batch-rows/{body['batch_row_id']}")
                if isinstance(row, dict) and row.get("status") in _ROW_TERMINAL:
                    return row["status"]
            elif source == "call_request" and body.get("request_id"):
                req = await lambda_invoke("GET", f"/call-requests/{body['request_id']}")
                if isinstance(req, dict) and req.get("status") in _CALL_REQUEST_TERMINAL:
                    return req["status"]
            else:
                # No entity to poll — release quickly
                return "unknown"

            await asyncio.sleep(TERMINAL_POLL_INTERVAL_SECS)

        # Timeout — force-mark as failed
        logger.warning(
            f"Consumer: wait-for-terminal timed out for {body.get('to_number')}"
        )
        await self._mark_failed(body, "Call timed out waiting for terminal status")
        return "timeout"

    # ── Status writes ───────────────────────────────────────────────────────

    async def _mark_dialing(self, body: dict[str, Any]) -> None:
        attempt = (body.get("attempt_count") or 0) + 1
        source = body.get("source") or "call_request"
        try:
            if source == "batch" and body.get("batch_row_id"):
                await lambda_invoke(
                    "PUT", f"/batch-rows/{body['batch_row_id']}",
                    body={"status": "dialing", "attempt_count": attempt},
                )
            elif source == "call_request" and body.get("request_id"):
                await lambda_invoke(
                    "PUT", f"/call-requests/{body['request_id']}",
                    body={"status": "dialing", "attempt_count": attempt},
                )
        except Exception:
            logger.exception("mark_dialing failed")

    async def _mark_failed(self, body: dict[str, Any], error_msg: str) -> None:
        source = body.get("source") or "call_request"
        try:
            if source == "batch" and body.get("batch_row_id"):
                await lambda_invoke(
                    "PUT", f"/batch-rows/{body['batch_row_id']}",
                    body={"status": "failed", "error_message": error_msg},
                )
            elif source == "call_request" and body.get("request_id"):
                await lambda_invoke(
                    "PUT", f"/call-requests/{body['request_id']}",
                    body={"status": "dead_letter", "error_message": error_msg},
                )
        except Exception:
            logger.exception("mark_failed failed")

    async def _mark_retry(self, body: dict[str, Any], attempt: int, error_msg: str) -> None:
        source = body.get("source") or "call_request"
        try:
            if source == "batch" and body.get("batch_row_id"):
                await lambda_invoke(
                    "PUT", f"/batch-rows/{body['batch_row_id']}",
                    body={
                        "status": "pending",
                        "attempt_count": attempt,
                        "error_message": error_msg,
                    },
                )
            elif source == "call_request" and body.get("request_id"):
                await lambda_invoke(
                    "PUT", f"/call-requests/{body['request_id']}",
                    body={
                        "status": "queued",
                        "attempt_count": attempt,
                        "error_message": error_msg,
                    },
                )
        except Exception:
            logger.exception("mark_retry failed")

    # ── Batch completion + results ──────────────────────────────────────────

    async def _check_batch_completion(self, batch_id: str) -> None:
        try:
            progress = await lambda_invoke(
                "GET", "/batch-rows/progress", query={"batch_id": batch_id}
            )
        except Exception:
            logger.exception(f"Progress fetch failed for batch {batch_id}")
            return
        if not isinstance(progress, dict):
            return

        pending = int(progress.get("pending") or 0)
        queued = int(progress.get("queued") or 0)
        dialing = int(progress.get("dialing") or 0)
        if pending or queued or dialing:
            return

        total = int(progress.get("total") or 0)
        completed = int(progress.get("completed") or 0)
        failed = int(progress.get("failed") or 0)

        try:
            await lambda_invoke("PUT", f"/batches/{batch_id}", body={
                "status": "completed",
                "completed_rows": completed,
                "failed_rows": failed,
                "total_rows": total,
            })
            logger.info(
                f"Batch {batch_id} completed: "
                f"{completed} successful, {failed} failed, {total} total"
            )
        except Exception:
            logger.exception(f"Batch {batch_id} completion update failed")

        # Build Excel results asynchronously — don't block consumer loop
        asyncio.create_task(_build_and_upload_results(batch_id))

    # ── SQS helpers ─────────────────────────────────────────────────────────

    async def _delete(self, receipt: str) -> None:
        try:
            await asyncio.to_thread(
                _sqs.delete_message,
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=receipt,
            )
        except Exception:
            logger.exception("SQS delete failed")

    async def _change_visibility(self, receipt: str, seconds: int) -> None:
        try:
            await asyncio.to_thread(
                _sqs.change_message_visibility,
                QueueUrl=SQS_QUEUE_URL,
                ReceiptHandle=receipt,
                VisibilityTimeout=seconds,
            )
        except Exception:
            logger.exception("SQS change-visibility failed")


# ── Calling-window helper ───────────────────────────────────────────────────


def _is_within_window(body: dict[str, Any]) -> bool:
    # New nested shape (batch source): body["calling_window"] = {start, end, timezone, days}
    window = body.get("calling_window")
    if isinstance(window, dict):
        tz_str = window.get("timezone") or "America/New_York"
        start = window.get("start") or "00:00"
        end = window.get("end") or "23:59"
        days = window.get("days") or ["Mon", "Tue", "Wed", "Thu", "Fri"]
    else:
        # Legacy flat shape (call_request source)
        tz_str = body.get("calling_window_timezone") or "America/New_York"
        start = body.get("calling_window_start") or "09:00"
        end = body.get("calling_window_end") or "17:00"
        days = body.get("calling_window_days") or ["Mon", "Tue", "Wed", "Thu", "Fri"]

    if isinstance(days, str):
        try:
            days = json.loads(days)
        except (ValueError, TypeError):
            days = ["Mon", "Tue", "Wed", "Thu", "Fri"]

    try:
        tz = ZoneInfo(tz_str)
        now = datetime.now(tz)
        if now.strftime("%a") not in days:
            return False
        return start <= now.strftime("%H:%M") <= end
    except Exception:
        return True


# ── Excel results builder (ported from old batch_runner) ────────────────────


async def _build_and_upload_results(batch_id: str) -> str | None:
    """Build an xlsx of batch results and upload to S3."""
    try:
        batch_data = await lambda_invoke("GET", f"/batches/{batch_id}")
        if not batch_data:
            return None
        batch = batch_data.get("batch", batch_data) if isinstance(batch_data, dict) else {}
        calls = (batch_data.get("calls") if isinstance(batch_data, dict) else []) or []

        agent_name = batch.get("agent_name", "")
        config = load_agent_config(agent_name) if agent_name else None
        analysis_names: list[str] = []
        if config and config.post_call_analyses:
            pca = config.post_call_analyses
            if hasattr(pca, "fields") and pca.fields:
                analysis_names = [f.name for f in pca.fields]
            elif isinstance(pca, list):
                analysis_names = [a.name for a in pca]

        rows_resp = await lambda_invoke(
            "GET", "/batch-rows", query={"batch_id": batch_id}
        )
        rows: list[dict[str, Any]] = []
        if isinstance(rows_resp, dict):
            rows = rows_resp.get("rows") or []
        elif isinstance(rows_resp, list):
            rows = rows_resp
        rows = sorted(rows, key=lambda r: r.get("row_index") or 0)

        calls_by_idx: dict[int, dict[str, Any]] = {}
        for c in calls:
            idx = c.get("batch_row_index")
            if idx is not None:
                calls_by_idx[int(idx)] = c

        wb = Workbook()
        ws = wb.active
        if ws is None:
            return None
        ws.title = "results"

        base_keys: list[str] = []
        if rows:
            cd = parse_jsonb(rows[0].get("case_data"), {}) or {}
            if isinstance(cd, dict):
                base_keys = list(cd.keys())

        headers = (
            list(base_keys)
            + ["phone_e164", "row_status", "call_id", "call_status", "duration_secs", "recording_url"]
            + list(analysis_names)
        )
        ws.append(headers)

        for r in rows:
            idx = int(r.get("row_index") or 0)
            cd = parse_jsonb(r.get("case_data"), {}) or {}
            phone = r.get("phone_e164", "")
            row_out: list[Any] = [cd.get(k, "") for k in base_keys] if isinstance(cd, dict) else [""] * len(base_keys)

            call = calls_by_idx.get(idx, {})
            post = call.get("post_call_analyses") or {}
            if isinstance(post, str):
                post = parse_jsonb(post, {}) or {}

            rec_path = call.get("recording_path") or ""
            rec_url = f"s3://{S3_BUCKET}/{rec_path}" if rec_path else ""

            row_out += [
                phone,
                r.get("status", ""),
                call.get("id", ""),
                call.get("status", ""),
                call.get("duration_secs", ""),
                rec_url,
            ]
            for name in analysis_names:
                row_out.append(post.get(name, "") if isinstance(post, dict) else "")
            ws.append(row_out)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        data = buf.read()
        out_key = f"batch-files/{batch_id}/results.xlsx"

        s3 = boto3.client("s3", region_name=AWS_REGION)
        await asyncio.to_thread(
            s3.put_object,
            Bucket=S3_BUCKET,
            Key=out_key,
            Body=data,
            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        await lambda_invoke("PUT", f"/batches/{batch_id}", body={"output_file_path": out_key})
        logger.info(f"Batch {batch_id} results uploaded to s3://{S3_BUCKET}/{out_key}")
        return out_key
    except Exception:
        logger.exception(f"Failed to build/upload batch {batch_id} results")
        return None


# Keep timezone import alive for older call_request payloads that reference UTC
_ = timezone
