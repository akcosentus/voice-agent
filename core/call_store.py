"""Supabase persistence for calls and batches (sync client via asyncio.to_thread)."""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime
from typing import Any, Optional

from core.call_result import CallResult
from core.supabase_client import get_supabase


def parse_jsonb(value: Any, default: Any = None) -> Any:
    """Safely parse a JSONB value that might be a string, None, or already parsed."""
    if default is None:
        default = []
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return _json.loads(value)
        except (_json.JSONDecodeError, TypeError):
            return default
    return default


def _dt_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _call_row_from_result(result: CallResult) -> dict[str, Any]:
    return {
        "id": result.call_id,
        "agent_name": result.agent_name,
        "agent_display_name": result.agent_display_name,
        "target_number": result.target_number,
        "direction": result.direction,
        "status": result.status,
        "started_at": _dt_iso(result.started_at),
        "ended_at": _dt_iso(result.ended_at),
        "duration_secs": result.duration_secs,
        "case_data": result.case_data,
        "transcript": result.transcript,
        "recording_path": result.recording_path,
        "post_call_analyses": result.post_call_analyses,
        "error": result.error,
        "batch_id": result.batch_id,
        "batch_row_index": result.batch_row_index,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


def _save_call_result_sync(result: CallResult) -> None:
    row = _call_row_from_result(result)
    get_supabase().table("calls").upsert(row).execute()


async def save_call_result(result: CallResult) -> None:
    await asyncio.to_thread(_save_call_result_sync, result)


def _update_call_sync(
    call_id: str,
    fields: dict[str, Any],
) -> None:
    fields = {**fields, "updated_at": datetime.utcnow().isoformat() + "Z"}
    get_supabase().table("calls").update(fields).eq("id", call_id).execute()


async def update_call_fields(call_id: str, **fields: Any) -> None:
    await asyncio.to_thread(_update_call_sync, call_id, fields)


def _insert_batch_sync(row: dict[str, Any]) -> dict[str, Any]:
    resp = get_supabase().table("batches").insert(row).execute()
    if resp.data and len(resp.data) > 0:
        return resp.data[0]
    return row


async def insert_batch(row: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_insert_batch_sync, row)


def _update_batch_sync(batch_id: str, fields: dict[str, Any]) -> None:
    fields = {**fields, "updated_at": datetime.utcnow().isoformat() + "Z"}
    get_supabase().table("batches").update(fields).eq("id", batch_id).execute()


async def update_batch(batch_id: str, **fields: Any) -> None:
    await asyncio.to_thread(_update_batch_sync, batch_id, fields)


def _get_batch_sync(batch_id: str) -> Optional[dict[str, Any]]:
    resp = get_supabase().table("batches").select("*").eq("id", batch_id).limit(1).execute()
    if resp.data and len(resp.data) > 0:
        return resp.data[0]
    return None


async def get_batch(batch_id: str) -> Optional[dict[str, Any]]:
    return await asyncio.to_thread(_get_batch_sync, batch_id)


def _get_batch_calls_sync(batch_id: str) -> list[dict[str, Any]]:
    resp = (
        get_supabase()
        .table("calls")
        .select("*")
        .eq("batch_id", batch_id)
        .execute()
    )
    return list(resp.data or [])


async def get_batch_calls(batch_id: str) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_get_batch_calls_sync, batch_id)


def _get_call_sync(call_id: str) -> Optional[dict[str, Any]]:
    resp = get_supabase().table("calls").select("*").eq("id", call_id).limit(1).execute()
    if resp.data and len(resp.data) > 0:
        return resp.data[0]
    return None


async def get_call(call_id: str) -> Optional[dict[str, Any]]:
    return await asyncio.to_thread(_get_call_sync, call_id)


def _increment_batch_counter_atomic_sync(batch_id: str, field: str) -> None:
    """Atomic counter increment via Supabase RPC to avoid race conditions."""
    try:
        get_supabase().rpc("increment_counter", {
            "batch_uuid": batch_id,
            "field_name": field,
            "amount": 1,
        }).execute()
    except Exception as e:
        from loguru import logger
        logger.warning(
            f"RPC increment_counter failed for batch {batch_id}/{field}, "
            f"using non-atomic fallback: {e}"
        )
        _increment_batch_counter_fallback_sync(batch_id, field)


def _increment_batch_counter_fallback_sync(batch_id: str, field: str) -> None:
    """Fallback read-then-write increment if RPC not available."""
    batch = _get_batch_sync(batch_id)
    if not batch:
        return
    current = int(batch.get(field) or 0)
    _update_batch_sync(batch_id, {field: current + 1})


async def increment_batch_counter(batch_id: str, field: str) -> None:
    """Increment completed_rows or failed_rows on a batch (atomic when RPC available)."""
    await asyncio.to_thread(_increment_batch_counter_atomic_sync, batch_id, field)
