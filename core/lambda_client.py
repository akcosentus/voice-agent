"""
Lambda API client for the MedCloud data layer.

The calling engine uses this to:
- Load agent configs before calls
- Load payer data and claim context
- Save call records after calls
- Trigger post-call auto-actions

Uses boto3 Lambda invoke (direct invocation) since API Gateway auth
is not yet configured. When LAMBDA_API_URL is set, switches to HTTP.
"""

import asyncio
import json
import logging
import os
from typing import Any, Optional

import httpx

from core.redaction import mask_phone

logger = logging.getLogger(__name__)

LAMBDA_FUNCTION = os.getenv("LAMBDA_FUNCTION_NAME", "medcloud-voice-api")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

LAMBDA_API_URL = os.getenv("LAMBDA_API_URL", "")  # set to switch to HTTP mode
LAMBDA_API_KEY = os.getenv("LAMBDA_API_KEY", "")

_NUMERIC_FIELDS = frozenset(
    ["temperature", "tts_stability", "tts_similarity_boost", "tts_style", "tts_speed"]
)


# ── Transport layer ──────────────────────────────────────────────────────────


def _get_boto3_client():
    import boto3

    return boto3.client("lambda", region_name=AWS_REGION)


def _invoke_lambda_sync(
    method: str, path: str, body: dict | None = None, query: dict | None = None
) -> dict:
    """Synchronous boto3 Lambda invoke.  Runs in a thread via the async wrapper."""
    event: dict[str, Any] = {
        "rawPath": f"/prod/voice/api{path}",
        "requestContext": {"http": {"method": method}},
        "queryStringParameters": query or {},
    }
    if body is not None:
        event["body"] = json.dumps(body)

    client = _get_boto3_client()
    resp = client.invoke(
        FunctionName=LAMBDA_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(event),
    )

    payload = json.loads(resp["Payload"].read())

    status = payload.get("statusCode", 200)
    body_str = payload.get("body", "{}")
    parsed = json.loads(body_str) if isinstance(body_str, str) else body_str

    if status >= 400:
        detail = parsed.get("detail", body_str[:200] if isinstance(body_str, str) else "")
        logger.error("Lambda %s %s → %s: %s", method, path, status, detail)
        return {"_error": True, "_status": status, "_detail": detail}

    return parsed


async def _invoke_http(
    method: str, path: str, body: dict | None = None, query: dict | None = None
) -> dict:
    """HTTP mode — used when LAMBDA_API_URL is configured."""
    url = f"{LAMBDA_API_URL.rstrip('/')}{path}"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if LAMBDA_API_KEY:
        headers["x-api-key"] = LAMBDA_API_KEY

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, url, json=body, params=query, headers=headers)

    if resp.status_code >= 400:
        detail = resp.text[:200]
        logger.error("HTTP %s %s → %s: %s", method, url, resp.status_code, detail)
        return {"_error": True, "_status": resp.status_code, "_detail": detail}

    return resp.json()


async def invoke(
    method: str, path: str, body: dict | None = None, query: dict | None = None
) -> dict:
    """Route to HTTP or boto3 depending on configuration."""
    try:
        if LAMBDA_API_URL:
            return await _invoke_http(method, path, body, query)
        return await asyncio.to_thread(_invoke_lambda_sync, method, path, body, query)
    except Exception as e:
        logger.error("Lambda invoke failed: %s %s: %s", method, path, e)
        raise


def _is_error(result) -> bool:
    return isinstance(result, dict) and result.get("_error", False)


# ── Agent config ─────────────────────────────────────────────────────────────


async def get_agent_config(agent_name: str) -> Optional[dict]:
    """Load full agent config. Returns None if not found."""
    result = await invoke("GET", f"/agents/{agent_name}")
    if not result or _is_error(result):
        return None

    for field in _NUMERIC_FIELDS:
        if field in result and isinstance(result[field], str):
            try:
                result[field] = float(result[field])
            except (ValueError, TypeError):
                pass

    return result


async def get_agent_draft(agent_name: str) -> Optional[dict]:
    """Load unpublished draft config for test calls."""
    result = await invoke("GET", f"/agent-drafts/{agent_name}")
    if not result or _is_error(result):
        return None

    for field in _NUMERIC_FIELDS:
        if field in result and isinstance(result[field], str):
            try:
                result[field] = float(result[field])
            except (ValueError, TypeError):
                pass

    return result


async def get_agent_schema() -> dict:
    """Load agent config schema (models, voices, field ranges)."""
    result = await invoke("GET", "/agent-schema")
    return {} if _is_error(result) else result


# ── Payer knowledge graph ────────────────────────────────────────────────────


async def get_payer(payer_id: str) -> Optional[dict]:
    """Load payer data including phone numbers, IVR paths, deadlines.

    Accepts either the UUID or the short payer_id (e.g. 'aetna').
    Falls back to a client-side list filter when the Lambda's type-cast
    on UUID comparison fails for string payer_ids.
    """
    result = await invoke("GET", f"/payers/{payer_id}")
    if not _is_error(result):
        return result

    # Fallback: the Lambda's WHERE id=$1 OR payer_id=$1 fails when
    # payer_id is a non-UUID string.  Filter from the full list instead.
    all_payers = await list_payers()
    for p in all_payers:
        if p.get("payer_id") == payer_id or p.get("id") == payer_id:
            return p
    return None


async def list_payers() -> list[dict]:
    """List all payers in the knowledge graph."""
    result = await invoke("GET", "/payers")
    if not result or _is_error(result):
        return []
    return result.get("payers", []) if isinstance(result, dict) else result


# ── Claim context ────────────────────────────────────────────────────────────


async def get_claim_context(claim_id: str) -> Optional[dict]:
    """Load full claim + patient + payer context for prompt hydration."""
    result = await invoke("GET", f"/claim-context/{claim_id}")
    return None if not result or _is_error(result) else result


# ── Call templates ───────────────────────────────────────────────────────────


async def get_call_template(template_name: str) -> Optional[dict]:
    """Load a pre-built RCM call flow template."""
    result = await invoke("GET", f"/call-templates/{template_name}")
    return None if not result or _is_error(result) else result


async def list_call_templates() -> list[dict]:
    """List all available call flow templates."""
    result = await invoke("GET", "/call-templates")
    if not result or _is_error(result):
        return []
    return result.get("templates", []) if isinstance(result, dict) else result


# ── Call records ─────────────────────────────────────────────────────────────


async def save_call_record(call_data: dict) -> Optional[str]:
    """Save a completed call record. Returns the call ID or None on failure."""
    result = await invoke("POST", "/calls", body=call_data)
    if not result or _is_error(result):
        logger.error(
            "Failed to save call record for %s",
            mask_phone(call_data.get("target_number")),
        )
        return None
    return result.get("id") if isinstance(result, dict) else None


async def trigger_auto_actions(call_id: str) -> Optional[dict]:
    """Trigger post-call intelligence (cost tracking, quality scoring, task creation, denial routing)."""
    result = await invoke("POST", "/auto-actions", body={"call_id": call_id})
    if not result or _is_error(result):
        logger.warning("Auto-actions failed for call %s", call_id)
        return None
    if isinstance(result, dict):
        logger.info(
            "Auto-actions for %s: %d actions, quality=%s, cost=$%s",
            call_id,
            result.get("actions_taken", 0),
            result.get("quality_score"),
            result.get("cost"),
        )
    return result


# ── Batches ──────────────────────────────────────────────────────────────────


async def get_batch(batch_id: str) -> Optional[dict]:
    """Load batch data including rows for the batch runner."""
    result = await invoke("GET", f"/batches/{batch_id}")
    return None if not result or _is_error(result) else result


async def get_batch_status(batch_id: str) -> Optional[dict]:
    """Get batch progress counters."""
    result = await invoke("GET", f"/batches/{batch_id}/status")
    return None if not result or _is_error(result) else result


async def update_batch_status(batch_id: str, action: str) -> Optional[dict]:
    """Transition batch status (start, pause, resume, cancel)."""
    result = await invoke("POST", f"/batches/{batch_id}/{action}")
    return None if not result or _is_error(result) else result


# ── Phone numbers ────────────────────────────────────────────────────────────


async def list_phone_numbers() -> list[dict]:
    """List all managed phone numbers with agent assignments."""
    result = await invoke("GET", "/phone-numbers")
    if not result or _is_error(result):
        return []
    return result.get("phone_numbers", []) if isinstance(result, dict) else result


async def get_inbound_routes() -> list[dict]:
    """List inbound call routing rules."""
    result = await invoke("GET", "/inbound-routes")
    if not result or _is_error(result):
        return []
    return result.get("routes", []) if isinstance(result, dict) else result
