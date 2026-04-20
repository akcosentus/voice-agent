"""Batch queue operations — start/pause/resume/cancel batches via SQS + voice_batch_rows.

No in-memory dialing. `start_batch` fans out rows to SQS; the unified consumer
processes them. Pause/cancel just flip flags — the consumer checks them
before dialing each message.
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from loguru import logger

from core.lambda_client import invoke as lambda_invoke

SQS_QUEUE_URL = os.getenv(
    "CALL_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/825269749545/medcloud-voice-call-requests",
)
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

_sqs = boto3.client("sqs", region_name=AWS_REGION)

STARTABLE_STATUSES = {"draft", "ready", "paused", "scheduled", "running"}


async def _get_system_concurrency() -> dict[str, int]:
    """Fetch concurrency limits from voice_system_config.

    Falls back to safe defaults if the key is missing.
    """
    try:
        cfg = await lambda_invoke("GET", "/system-config/concurrency")
        if isinstance(cfg, dict):
            val = cfg.get("value") or {}
            if isinstance(val, dict):
                return {
                    "global_max": int(val.get("global_max", 5)),
                    "per_batch_max": int(val.get("per_batch_max", 5)),
                    "queue_max": int(val.get("queue_max", 2)),
                }
    except Exception:
        logger.warning("Failed to load system concurrency — using defaults")
    return {"global_max": 5, "per_batch_max": 5, "queue_max": 2}


async def start_batch(batch_id: str) -> dict[str, Any]:
    """Idempotently start a batch: send all pending rows to SQS.

    Returns a dict describing the outcome — caller should forward it to the HTTP client.
    """
    # 1. Load batch
    batch_data = await lambda_invoke("GET", f"/batches/{batch_id}")
    if not batch_data:
        return {"error": "Batch not found", "batch_id": batch_id, "http_status": 404}
    batch = batch_data.get("batch", batch_data) if isinstance(batch_data, dict) else {}

    status = batch.get("status")
    if status not in STARTABLE_STATUSES:
        return {
            "error": f"Batch not in startable state (current: {status})",
            "batch_id": batch_id,
            "http_status": 409,
        }

    agent_name = batch.get("agent_name")
    from_number = batch.get("from_number")
    if not agent_name or not from_number:
        return {
            "error": "Batch missing agent_name or from_number",
            "batch_id": batch_id,
            "http_status": 400,
        }

    # 2. Clear any user pause flag from a previous run
    try:
        await lambda_invoke("PUT", f"/batches/{batch_id}", body={
            "status": "running",
            "paused_by_user": False,
        })
    except Exception:
        logger.warning(f"start_batch: failed to flip status for {batch_id}")

    # 3. Concurrency (informational — the consumer enforces global_max)
    sys_cc = await _get_system_concurrency()
    batch_concurrency = min(
        int(batch.get("concurrency") or 5),
        sys_cc["per_batch_max"],
    )

    calling_window = {
        "start": batch.get("calling_window_start") or "00:00",
        "end": batch.get("calling_window_end") or "23:59",
        "timezone": batch.get("timezone") or "America/New_York",
        "days": batch.get("calling_window_days") or ["Mon", "Tue", "Wed", "Thu", "Fri"],
    }

    # 4. Fetch pending rows
    rows_resp = await lambda_invoke(
        "GET", "/batch-rows", query={"batch_id": batch_id, "status": "pending"}
    )
    rows: list[dict[str, Any]] = []
    if isinstance(rows_resp, dict):
        rows = rows_resp.get("rows") or []
    elif isinstance(rows_resp, list):
        rows = rows_resp

    if not rows:
        logger.info(f"Batch {batch_id}: no pending rows — marking completed")
        await lambda_invoke("PUT", f"/batches/{batch_id}", body={"status": "completed"})
        return {"status": "completed", "message": "No pending rows", "queued": 0}

    # 5. Send to SQS + mark rows queued
    queued = 0
    failures: list[str] = []
    for row in rows:
        payload = {
            "source": "batch",
            "batch_id": batch_id,
            "batch_row_id": row["id"],
            "row_index": row.get("row_index"),
            "agent_name": agent_name,
            "to_number": row["phone_e164"],
            "from_number": from_number,
            "case_data": row.get("case_data") or {},
            "concurrency": batch_concurrency,
            "calling_window": calling_window,
            "dedup_key": f"batch_{batch_id}_row_{row.get('row_index')}",
            "attempt_count": row.get("attempt_count") or 0,
            "max_attempts": row.get("max_attempts") or 3,
        }
        try:
            await _send_sqs(payload)
            await lambda_invoke("PUT", f"/batch-rows/{row['id']}", body={"status": "queued"})
            queued += 1
        except Exception as exc:
            logger.exception(f"Batch {batch_id} row {row.get('row_index')}: SQS send failed")
            failures.append(f"row {row.get('row_index')}: {exc}")

    logger.info(
        f"Batch {batch_id}: queued {queued}/{len(rows)} rows "
        f"(concurrency={batch_concurrency})"
    )
    return {
        "status": "running",
        "batch_id": batch_id,
        "queued": queued,
        "total": len(rows),
        "failures": failures if failures else None,
    }


async def pause_batch(batch_id: str) -> dict[str, Any]:
    await lambda_invoke("PUT", f"/batches/{batch_id}", body={
        "paused_by_user": True,
        "status": "paused",
    })
    return {"status": "paused", "batch_id": batch_id}


async def resume_batch(batch_id: str) -> dict[str, Any]:
    await lambda_invoke("PUT", f"/batches/{batch_id}", body={
        "paused_by_user": False,
        "status": "running",
    })
    return {"status": "running", "batch_id": batch_id}


async def cancel_batch(batch_id: str) -> dict[str, Any]:
    """Mark all pending + queued rows as cancelled, flip batch status."""
    cancelled_rows = 0
    for status_filter in ("pending", "queued"):
        try:
            resp = await lambda_invoke(
                "GET", "/batch-rows", query={"batch_id": batch_id, "status": status_filter}
            )
            rows = (resp.get("rows") if isinstance(resp, dict) else resp) or []
            for row in rows:
                try:
                    await lambda_invoke(
                        "PUT", f"/batch-rows/{row['id']}", body={"status": "cancelled"}
                    )
                    cancelled_rows += 1
                except Exception:
                    logger.exception(f"Failed to cancel row {row.get('id')}")
        except Exception:
            logger.exception(f"Failed to list {status_filter} rows for cancel")

    await lambda_invoke("PUT", f"/batches/{batch_id}", body={"status": "cancelled"})
    return {"status": "cancelled", "batch_id": batch_id, "cancelled_rows": cancelled_rows}


async def _send_sqs(payload: dict[str, Any]) -> None:
    """Send a message to SQS (sync boto3 wrapped in to_thread)."""
    import asyncio as _asyncio

    await _asyncio.to_thread(
        _sqs.send_message,
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps(payload),
    )
