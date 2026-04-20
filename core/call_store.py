"""Call persistence via Lambda API — saves call records and triggers post-call intelligence."""

from __future__ import annotations

import json as _json
from datetime import datetime
from typing import Any, Optional

from loguru import logger

from core.call_result import CallResult


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
        "duration_secs": int(result.duration_secs) if result.duration_secs else 0,
        "case_data": result.case_data,
        "transcript": result.transcript,
        "recording_path": result.recording_path,
        "post_call_analyses": result.post_call_analyses,
        "error": result.error,
        "batch_id": result.batch_id,
        "batch_row_index": result.batch_row_index,
        "batch_row_id": result.batch_row_id,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }


async def save_call_result(result: CallResult) -> None:
    """Save call record to Lambda and trigger auto-actions if call is terminal."""
    from core.lambda_client import save_call_record, trigger_auto_actions

    row = _call_row_from_result(result)
    call_id = await save_call_record(row)

    if call_id and result.status in ("completed", "failed", "no_answer", "busy"):
        try:
            await trigger_auto_actions(call_id)
        except Exception:
            logger.exception("Auto-actions failed for call %s", call_id)


async def update_call_fields(call_id: str, **fields: Any) -> None:
    """Update specific fields on a call record via Lambda."""
    from core.lambda_client import save_call_record

    fields["id"] = call_id
    fields["updated_at"] = datetime.utcnow().isoformat() + "Z"
    await save_call_record(fields)
