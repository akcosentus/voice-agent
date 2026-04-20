"""FastAPI calling engine + Lambda proxy.

This server does two things:
1. Real-time voice calls: Twilio WebSocket, WebRTC test calls, outbound dialing
2. Lambda proxy: forwards frontend data requests to the MedCloud Lambda API
   (the frontend can't call Lambda directly due to API Gateway auth)
"""

import asyncio
import json as _json
import os
import uuid
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.responses import HTMLResponse
from loguru import logger
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
    SmallWebRTCPatchRequest,
    IceCandidate,
    IceServer,
)

from core.config_loader import load_agent_config
from core.lambda_client import (
    get_agent_config,
    get_agent_draft,
    get_agent_schema,
    get_batch,
    get_call_template,
    get_claim_context,
    get_payer,
    invoke,
    list_call_templates,
    list_payers,
    list_phone_numbers,
)
from server.batch_queue import (
    cancel_batch as _cancel_batch,
    pause_batch as _pause_batch,
    resume_batch as _resume_batch,
    start_batch as _start_batch,
)
from server.schemas import OutboundCallRequest, OutboundCallResponse
from server.twilio_outbound import place_outbound_call, ws_url_from_env
from server.ws_handler import handle_twilio_websocket

app = FastAPI(title="MedCloud Voice Engine")

from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_API_KEY = os.getenv("COSENTUS_API_KEY", "")
_AUTH_EXEMPT = frozenset({
    "/health",
    "/twilio/incoming",
    "/api/twilio/status-callback",
    "/ws",
    "/api/test-call/connect",
})


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not _API_KEY:
            return await call_next(request)
        path = request.url.path
        if path in _AUTH_EXEMPT or path.startswith("/ws") or request.method == "OPTIONS":
            return await call_next(request)
        key = request.headers.get("x-api-key", "")
        if key != _API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})
        return await call_next(request)


app.add_middleware(APIKeyMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_pending_calls: dict[str, dict] = {}
_webrtc_handler = SmallWebRTCRequestHandler(
    ice_servers=[IceServer(urls="stun:stun.l.google.com:19302")],
)

S3_BUCKET = os.getenv("S3_BUCKET", "medcloud-voice-us-prod-825")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# ── Background services (consumer + CloudWatch heartbeat) ───────────────────

from server.call_queue_consumer import UnifiedCallConsumer

_queue_consumer = UnifiedCallConsumer(_pending_calls)
_cloudwatch = None


async def _health_heartbeat() -> None:
    """Push a 1-per-minute HealthCheck metric to CloudWatch."""
    global _cloudwatch
    try:
        import boto3
        _cloudwatch = boto3.client("cloudwatch", region_name=AWS_REGION)
    except Exception:
        logger.warning("CloudWatch client init failed — heartbeat disabled")
        return
    while True:
        try:
            await asyncio.to_thread(
                _cloudwatch.put_metric_data,
                Namespace="VoiceEngine",
                MetricData=[{"MetricName": "HealthCheck", "Value": 1, "Unit": "Count"}],
            )
        except Exception as exc:
            logger.warning(f"Health heartbeat push failed: {exc}")
        await asyncio.sleep(60)


@app.on_event("startup")
async def _on_startup() -> None:
    asyncio.create_task(_queue_consumer.start())
    asyncio.create_task(_health_heartbeat())
    logger.info("Background services started: queue consumer + CloudWatch heartbeat")


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    _queue_consumer.stop()


def _is_uuid(val: str) -> bool:
    try:
        uuid.UUID(val)
        return True
    except (ValueError, AttributeError):
        return False


async def validate_agent_ready(agent_name: str) -> list[str]:
    """Check agent config is complete enough to make a call."""
    errors: list[str] = []
    try:
        config = load_agent_config(agent_name)
    except Exception as e:
        return [f"Failed to load agent config: {e}"]
    if not config:
        return [f"Agent '{agent_name}' not found"]
    if not config.system_prompt or len(config.system_prompt.strip()) < 10:
        errors.append("Agent has no system prompt (or it's too short)")
    if not config.tts.voice_id:
        errors.append("Agent has no TTS voice configured")
    if not config.llm.model:
        errors.append("Agent has no LLM model configured")
    if not config.tts.model:
        errors.append("Agent has no TTS model configured")
    return errors


# ── Lambda proxy helper ─────────────────────────────────────────────────────


async def _proxy(
    method: str,
    path: str,
    body: dict | None = None,
    query: dict | None = None,
):
    """Forward a request to the Lambda and return the result.

    Converts Lambda _error responses into proper HTTPExceptions.
    """
    result = await invoke(method, path, body=body, query=query)
    if isinstance(result, dict) and result.get("_error"):
        raise HTTPException(
            status_code=result.get("_status", 502),
            detail=result.get("_detail", "Lambda request failed"),
        )
    return result


def _s3_client():
    import boto3

    return boto3.client("s3", region_name=AWS_REGION)


async def _s3_presigned_url(key: str, expires_in: int = 3600) -> str:
    s3 = _s3_client()
    return await asyncio.to_thread(
        s3.generate_presigned_url,
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )


# ── Health check ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "service": "voice-engine"}


@app.get("/api/system/status")
async def system_status():
    """Consumer + concurrency status for monitoring."""
    return {
        "consumer": _queue_consumer.status,
        "health": "ok",
    }


# ═════════════════════════════════════════════════════════════════════════════
# PROXY: Agent endpoints → Lambda
# ═════════════════════════════════════════════════════════════════════════════


@app.get("/api/agent-schema")
async def proxy_agent_schema():
    return await get_agent_schema() or {}


@app.get("/api/agents")
async def proxy_list_agents():
    return await _proxy("GET", "/agents")


@app.post("/api/agents")
async def proxy_create_agent(request: Request):
    body = await request.json()
    result = await _proxy("POST", "/agents", body=body)
    agent_name = result.get("name") if isinstance(result, dict) else None
    if agent_name:
        try:
            await _proxy("POST", "/agent-drafts", body={
                "agent_id": agent_name,
                "name": agent_name,
                **{k: v for k, v in result.items() if k not in ("id", "created_at", "updated_at")},
                "has_unpublished_changes": False,
            })
        except Exception:
            logger.warning("Auto-draft creation failed for %s", agent_name)
    return result


@app.get("/api/agents/{agent_name:path}/draft")
async def proxy_get_agent_draft(agent_name: str):
    result = await get_agent_draft(agent_name)
    if result:
        return result
    live = await invoke("GET", f"/agents/{agent_name}")
    if not live or (isinstance(live, dict) and live.get("_error")):
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_name}")
    try:
        draft_body = {
            "agent_id": agent_name,
            "name": agent_name,
            **{k: v for k, v in live.items() if k not in ("id", "created_at", "updated_at")},
            "has_unpublished_changes": False,
        }
        created = await _proxy("POST", "/agent-drafts", body=draft_body)
        return created if created else draft_body
    except Exception:
        logger.warning("Auto-draft creation on read failed for %s", agent_name)
        return {**live, "agent_id": agent_name, "has_unpublished_changes": False}


@app.put("/api/agents/{agent_name:path}/draft")
async def proxy_update_agent_draft(agent_name: str, request: Request):
    body = await request.json()
    body.setdefault("agent_id", agent_name)
    body.setdefault("name", agent_name)
    return await _proxy("POST", "/agent-drafts", body=body)


@app.post("/api/agents/{agent_name:path}/publish")
async def proxy_publish_agent(agent_name: str):
    return await _proxy("POST", f"/agent-versions", body={"agent_name": agent_name})


@app.get("/api/agents/{agent_name:path}/prompt")
async def proxy_get_agent_prompt(agent_name: str):
    result = await get_agent_config(agent_name)
    if not result:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_name}")
    import re

    content = result.get("system_prompt", "")
    variables = sorted(set(re.findall(r"\{\{(\w+)\}\}", content)))
    return {"content": content, "prompt_variables": variables}


@app.put("/api/agents/{agent_name:path}/prompt")
async def proxy_update_agent_prompt(agent_name: str, request: Request):
    body = await request.json()
    return await _proxy("PUT", f"/agents/{agent_name}", body={"system_prompt": body.get("content", "")})


@app.post("/api/agents/{agent_name:path}/clone")
async def proxy_clone_agent(agent_name: str, request: Request):
    body = await request.json()
    result = await _proxy("POST", f"/agents/{agent_name}/clone", body=body)
    clone_name = result.get("name") if isinstance(result, dict) else None
    if clone_name:
        try:
            await _proxy("POST", "/agent-drafts", body={
                "agent_id": clone_name,
                "name": clone_name,
                **{k: v for k, v in result.items() if k not in ("id", "created_at", "updated_at")},
                "has_unpublished_changes": False,
            })
        except Exception:
            logger.warning("Auto-draft creation failed for clone %s", clone_name)
    return result


@app.post("/api/agents/{agent_name:path}/versions")
async def proxy_create_agent_version(agent_name: str, request: Request):
    body = await request.json()
    body.setdefault("agent_id", agent_name)
    return await _proxy("POST", "/agent-versions", body=body)


@app.get("/api/agents/{agent_name:path}/versions/{version_id}")
async def proxy_get_agent_version(agent_name: str, version_id: str):
    return await _proxy("GET", f"/agent-versions/{agent_name}/{version_id}")


@app.get("/api/agents/{agent_name:path}/versions")
async def proxy_list_agent_versions(agent_name: str):
    return await _proxy("GET", f"/agent-versions/{agent_name}")


@app.get("/api/agents/{agent_name:path}")
async def proxy_get_agent(agent_name: str):
    result = await get_agent_config(agent_name)
    if not result:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_name}")
    return result


@app.put("/api/agents/{agent_name:path}")
async def proxy_update_agent(agent_name: str, request: Request):
    return await _proxy("PUT", f"/agents/{agent_name}", body=await request.json())


@app.delete("/api/agents/{agent_name:path}")
async def proxy_delete_agent(agent_name: str):
    return await _proxy("DELETE", f"/agents/{agent_name}")


# ═════════════════════════════════════════════════════════════════════════════
# PROXY: Call endpoints → Lambda
# ═════════════════════════════════════════════════════════════════════════════


@app.get("/api/calls/agents")
async def proxy_call_agent_names():
    return await _proxy("GET", "/calls/agents")


@app.get("/api/calls/{call_id}/recording-url")
async def proxy_recording_url(call_id: str):
    call = await _proxy("GET", f"/calls/{call_id}")
    rec_path = call.get("recording_path") if isinstance(call, dict) else None
    if not rec_path:
        raise HTTPException(status_code=404, detail="No recording for this call")
    url = await _s3_presigned_url(rec_path)
    return {"url": url}


@app.put("/api/calls/{call_id}/hide")
@app.delete("/api/calls/{call_id}")
async def proxy_hide_call(call_id: str):
    """Soft-delete a call. Accepts PUT /api/calls/{id}/hide or DELETE /api/calls/{id}."""
    return await _proxy("PUT", f"/calls/{call_id}/hide")


@app.get("/api/calls/{call_id}")
async def proxy_get_call(call_id: str):
    return await _proxy("GET", f"/calls/{call_id}")


@app.get("/api/calls")
async def proxy_list_calls(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    agent_name: Optional[str] = None,
    agent_display_name: Optional[str] = None,
    batch_id: Optional[str] = None,
    status: Optional[str] = None,
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
):
    query: dict[str, str] = {
        "page": str(page),
        "page_size": str(page_size),
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    if agent_name:
        query["agent_name"] = agent_name
    if agent_display_name:
        query["agent_display_name"] = agent_display_name
    if batch_id:
        query["batch_id"] = batch_id
    if status:
        query["status"] = status
    return await _proxy("GET", "/calls", query=query)


# ═════════════════════════════════════════════════════════════════════════════
# PROXY: Batch endpoints → Lambda
# ═════════════════════════════════════════════════════════════════════════════


@app.post("/api/batches/upload")
async def proxy_upload_batch(
    file: UploadFile = File(...),
    agent_name: str = Form(...),
    from_number: str = Form(...),
):
    """Receive file, parse rows, upload raw file to S3, create batch + rows in Lambda."""
    from server.batch_parse import parse_upload_file, build_row_payloads

    raw = await file.read()
    fname = file.filename or "upload.xlsx"

    try:
        headers, rows = await asyncio.to_thread(parse_upload_file, raw, fname)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not rows:
        raise HTTPException(status_code=400, detail="File is empty or has no data rows")

    _phone_col_idx, row_payloads, summary = build_row_payloads(headers, rows)

    batch_id = str(uuid.uuid4())
    ext = os.path.splitext(fname)[1] or ".xlsx"
    s3_key = f"batch-files/{batch_id}/input{ext}"

    s3 = _s3_client()
    await asyncio.to_thread(
        s3.put_object, Bucket=S3_BUCKET, Key=s3_key, Body=raw,
        ContentType=file.content_type or "application/octet-stream",
    )

    agent_display = agent_name
    try:
        agent_data = await invoke("GET", f"/agents/{agent_name}")
        if agent_data and isinstance(agent_data, dict):
            agent_display = agent_data.get("display_name") or agent_name
    except Exception:
        pass

    # Create batch metadata (no rows JSONB anymore)
    valid_rows = [r for r in row_payloads if r.get("validation") == "valid"]
    batch = await _proxy("POST", "/batches", body={
        "id": batch_id,
        "name": fname,
        "agent_name": agent_name,
        "agent_display_name": agent_display,
        "from_number": from_number,
        "input_file_path": s3_key,
        "total_rows": len(valid_rows),
        "status": "draft",
    })

    # Insert valid rows into voice_batch_rows
    if valid_rows:
        await _proxy("POST", "/batch-rows", body={
            "batch_id": batch_id,
            "rows": [
                {
                    "row_index": r["row_index"],
                    "phone_e164": r["phone_e164"],
                    "case_data": r.get("case_data") or {},
                }
                for r in valid_rows
            ],
        })

    return {
        **(batch if isinstance(batch, dict) else {}),
        "batch_id": batch_id,
        "columns": headers,
        "summary": summary,
        "rows": row_payloads,
    }


@app.delete("/api/batches/cleanup-drafts")
async def proxy_cleanup_draft_batches():
    return await _proxy("DELETE", "/batches/cleanup-drafts")


@app.put("/api/batches/{batch_id}/rows")
async def proxy_update_batch_rows(batch_id: str, request: Request):
    return await _proxy("PUT", f"/batches/{batch_id}/rows", body=await request.json())


@app.post("/api/batches/{batch_id}/start")
async def proxy_start_batch(batch_id: str, request: Request):
    """Start a batch: persist schedule metadata, then fan rows into SQS."""
    body = await request.json()

    # Persist any schedule/concurrency overrides on the batch BEFORE fanning out.
    # We write via PUT /batches/:id to avoid Lambda's POST /start (which flips
    # status to running prematurely — start_batch does that itself).
    schedule_fields: dict = {}
    for key in (
        "concurrency",
        "timezone",
        "calling_window_start",
        "calling_window_end",
        "calling_window_days",
    ):
        if body.get(key) is not None:
            schedule_fields[key] = body[key]
    if schedule_fields:
        try:
            await _proxy("PUT", f"/batches/{batch_id}", body=schedule_fields)
        except HTTPException:
            logger.exception("Schedule pre-write failed (non-fatal, continuing)")

    result = await _start_batch(batch_id)
    if isinstance(result, dict) and "http_status" in result:
        raise HTTPException(status_code=result.pop("http_status"), detail=result)
    return result


@app.post("/api/batches/{batch_id}/pause")
async def proxy_pause_batch(batch_id: str):
    return await _pause_batch(batch_id)


@app.post("/api/batches/{batch_id}/resume")
async def proxy_resume_batch(batch_id: str):
    # Flip the flags, then re-fan any rows still pending (in case they were never queued)
    await _resume_batch(batch_id)
    result = await _start_batch(batch_id)
    if isinstance(result, dict) and "http_status" in result:
        raise HTTPException(status_code=result.pop("http_status"), detail=result)
    return result


@app.post("/api/batches/{batch_id}/cancel")
async def proxy_cancel_batch(batch_id: str):
    return await _cancel_batch(batch_id)


@app.get("/api/batches/{batch_id}/status")
async def proxy_batch_status(batch_id: str):
    return await _proxy("GET", f"/batches/{batch_id}/status")


@app.get("/api/batches/{batch_id}/results")
async def proxy_batch_results(batch_id: str):
    """Return a presigned S3 URL for the results file."""
    batch_data = await get_batch(batch_id)
    if not batch_data:
        raise HTTPException(status_code=404, detail="Batch not found")
    batch = batch_data.get("batch", batch_data)
    out_path = batch.get("output_file_path")
    if not out_path:
        raise HTTPException(status_code=404, detail="Results not ready yet")
    url = await _s3_presigned_url(out_path)
    return {"url": url}


@app.get("/api/batches/{batch_id}/download-url")
async def proxy_batch_download_url(batch_id: str):
    """Presigned S3 URL for the original uploaded file."""
    batch_data = await get_batch(batch_id)
    if not batch_data:
        raise HTTPException(status_code=404, detail="Batch not found")
    batch = batch_data.get("batch", batch_data)
    path = batch.get("input_file_path")
    if not path:
        raise HTTPException(status_code=404, detail="No file for this batch")
    url = await _s3_presigned_url(path)
    return {"url": url}


@app.delete("/api/batches/{batch_id}/draft")
async def proxy_delete_draft_batch(batch_id: str):
    return await _proxy("DELETE", f"/batches/{batch_id}/draft")


@app.get("/api/batches/{batch_id}")
async def proxy_get_batch(batch_id: str):
    return await _proxy("GET", f"/batches/{batch_id}")


@app.get("/api/batches")
async def proxy_list_batches():
    return await _proxy("GET", "/batches")


# ═════════════════════════════════════════════════════════════════════════════
# PROXY: Batch rows + system config → Lambda
# ═════════════════════════════════════════════════════════════════════════════


@app.get("/api/batch-rows/progress")
async def proxy_batch_row_progress(batch_id: str):
    return await _proxy("GET", "/batch-rows/progress", query={"batch_id": batch_id})


@app.get("/api/batch-rows")
async def proxy_list_batch_rows(
    batch_id: str,
    status: str | None = None,
    limit: int | None = None,
):
    params: dict[str, str] = {"batch_id": batch_id}
    if status:
        params["status"] = status
    if limit:
        params["limit"] = str(limit)
    return await _proxy("GET", "/batch-rows", query=params)


@app.post("/api/batch-rows")
async def proxy_create_batch_rows(request: Request):
    return await _proxy("POST", "/batch-rows", body=await request.json())


@app.get("/api/batch-rows/{row_id}")
async def proxy_get_batch_row(row_id: str):
    return await _proxy("GET", f"/batch-rows/{row_id}")


@app.put("/api/batch-rows/{row_id}")
async def proxy_update_batch_row(row_id: str, request: Request):
    return await _proxy("PUT", f"/batch-rows/{row_id}", body=await request.json())


@app.get("/api/system-config/{key}")
async def proxy_get_system_config(key: str):
    return await _proxy("GET", f"/system-config/{key}")


@app.put("/api/system-config/{key}")
async def proxy_set_system_config(key: str, request: Request):
    return await _proxy("PUT", f"/system-config/{key}", body=await request.json())


@app.post("/api/batches/{batch_id}/execute")
async def execute_batch(batch_id: str):
    """Alias for /start — fan batch rows into SQS for the unified consumer."""
    result = await _start_batch(batch_id)
    if isinstance(result, dict) and "http_status" in result:
        raise HTTPException(status_code=result.pop("http_status"), detail=result)
    return result


# ═════════════════════════════════════════════════════════════════════════════
# PROXY: Phone number endpoints → Lambda
# ═════════════════════════════════════════════════════════════════════════════


@app.get("/api/phone-numbers")
async def proxy_list_phones():
    return await _proxy("GET", "/phone-numbers")


@app.post("/api/phone-numbers/sync-twilio")
async def sync_twilio_numbers():
    """Pull phone numbers from the Twilio account and upsert into Aurora."""
    from twilio.rest import Client as TwilioClient

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")

    client = TwilioClient(sid, token)
    twilio_numbers = await asyncio.to_thread(client.incoming_phone_numbers.list)

    synced = []
    errors = []
    for num in twilio_numbers:
        data = {
            "number": num.phone_number,
            "friendly_name": num.friendly_name or num.phone_number,
        }
        try:
            await _proxy("POST", "/phone-numbers", body=data)
            synced.append(data)
        except Exception as e:
            errors.append({"number": num.phone_number, "error": str(e)})

    return {
        "status": "synced",
        "synced": len(synced),
        "errors": len(errors),
        "numbers": synced,
        "error_details": errors if errors else None,
    }


@app.post("/api/phone-numbers")
async def proxy_create_phone(request: Request):
    return await _proxy("POST", "/phone-numbers", body=await request.json())


@app.put("/api/phone-numbers/{phone_id}")
async def proxy_update_phone(phone_id: str, request: Request):
    body = await request.json()
    for field in ("inbound_agent_id", "outbound_agent_id"):
        val = body.get(field)
        if val and not _is_uuid(val):
            agent = await invoke("GET", f"/agents/{val}")
            if agent and isinstance(agent, dict) and agent.get("id"):
                body[field] = agent["id"]
            else:
                body[field] = None
    return await _proxy("PUT", f"/phone-numbers/{phone_id}", body=body)


@app.delete("/api/phone-numbers/{phone_id}")
async def proxy_delete_phone(phone_id: str):
    return await _proxy("DELETE", f"/phone-numbers/{phone_id}")


# ═════════════════════════════════════════════════════════════════════════════
# PROXY: Call request queue → Lambda
# ═════════════════════════════════════════════════════════════════════════════


@app.post("/api/call-requests")
async def proxy_create_call_request(request: Request):
    body = await request.json()
    validation_errors = await validate_agent_ready(body.get("agent_name", ""))
    if validation_errors:
        raise HTTPException(status_code=400, detail={
            "error": "Agent not ready for calls",
            "issues": validation_errors,
        })
    return await _proxy("POST", "/call-requests", body=body)


@app.get("/api/call-requests")
async def proxy_list_call_requests(
    status: str | None = None,
    agent_name: str | None = None,
    trigger_source: str | None = None,
):
    params = {}
    if status:
        params["status"] = status
    if agent_name:
        params["agent_name"] = agent_name
    if trigger_source:
        params["trigger_source"] = trigger_source
    return await _proxy("GET", "/call-requests", query=params)


@app.get("/api/call-requests/{request_id}")
async def proxy_get_call_request(request_id: str):
    return await _proxy("GET", f"/call-requests/{request_id}")


# ═════════════════════════════════════════════════════════════════════════════
# PROXY: Voice endpoints → Lambda
# ═════════════════════════════════════════════════════════════════════════════


@app.get("/api/voices")
async def proxy_list_voices():
    return await _proxy("GET", "/voices")


@app.post("/api/voices/lookup")
async def proxy_voice_lookup(request: Request):
    body = await request.json()
    try:
        result = await _proxy("POST", "/voices/lookup", body=body)
        if isinstance(result, dict) and result.get("voice_id"):
            return result
    except HTTPException:
        pass
    voice_id = body.get("voice_id", "")
    if not voice_id:
        raise HTTPException(status_code=400, detail="voice_id required")
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.elevenlabs.io/v1/voices/{voice_id}",
            headers={"xi-api-key": os.getenv("ELEVENLABS_API_KEY", "")},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=404, detail="Voice not found on ElevenLabs")
        v = resp.json()
        labels = v.get("labels", {})
        return {
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "description": v.get("description"),
            "preview_url": v.get("preview_url"),
            "gender": labels.get("gender"),
            "accent": labels.get("accent"),
            "age": labels.get("age"),
            "category": v.get("category"),
            "labels": labels,
        }


async def _fetch_elevenlabs_voice(voice_id: str) -> dict | None:
    """Fetch voice metadata from ElevenLabs API."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.elevenlabs.io/v1/voices/{voice_id}",
                headers={"xi-api-key": os.getenv("ELEVENLABS_API_KEY", "")},
            )
            if resp.status_code != 200:
                return None
            v = resp.json()
            labels = v.get("labels", {})
            return {
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "description": v.get("description"),
                "preview_url": v.get("preview_url"),
                "gender": labels.get("gender"),
                "accent": labels.get("accent"),
                "age": labels.get("age"),
                "category": v.get("category"),
                "labels": labels,
            }
    except Exception:
        return None


@app.post("/api/voices/add")
async def proxy_voice_add(request: Request):
    body = await request.json()
    voice_id = body.get("voice_id", "")
    meta = await _fetch_elevenlabs_voice(voice_id) if voice_id else None
    if meta:
        body.update({k: v for k, v in meta.items() if v is not None and k not in ("voice_id",)})
    return await _proxy("POST", "/voices/add", body=body)


@app.post("/api/voices/{voice_id}/refresh")
async def proxy_voice_refresh(voice_id: str):
    meta = await _fetch_elevenlabs_voice(voice_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Voice not found on ElevenLabs")
    await _proxy("POST", "/voices/add", body=meta)
    return meta


@app.get("/api/voices/{voice_id}/agents")
async def proxy_voice_agents(voice_id: str):
    return await _proxy("GET", f"/voices/{voice_id}/agents")


@app.get("/api/voices/{voice_id}")
async def proxy_get_voice(voice_id: str):
    return await _proxy("GET", f"/voices/{voice_id}")


@app.delete("/api/voices/{voice_id}")
async def proxy_delete_voice(voice_id: str):
    return await _proxy("DELETE", f"/voices/{voice_id}")


# ═════════════════════════════════════════════════════════════════════════════
# PROXY: Payer & template endpoints → Lambda (new data)
# ═════════════════════════════════════════════════════════════════════════════


@app.get("/api/payers")
async def proxy_list_payers():
    return await list_payers() or []


@app.get("/api/payers/{payer_id}")
async def proxy_get_payer(payer_id: str):
    result = await get_payer(payer_id)
    if not result:
        raise HTTPException(status_code=404, detail="Payer not found")
    return result


@app.get("/api/claim-context/{claim_id}")
async def proxy_get_claim_context(claim_id: str):
    result = await get_claim_context(claim_id)
    if not result:
        raise HTTPException(status_code=404, detail="Claim context not found")
    return result


@app.get("/api/call-templates")
async def proxy_list_templates():
    return await list_call_templates() or []


@app.get("/api/call-templates/{template_name}")
async def proxy_get_template(template_name: str):
    result = await get_call_template(template_name)
    if not result:
        raise HTTPException(status_code=404, detail="Template not found")
    return result


# ═════════════════════════════════════════════════════════════════════════════
# CALLING ENGINE: Outbound calls
# ═════════════════════════════════════════════════════════════════════════════


@app.post("/api/calls/outbound", response_model=OutboundCallResponse)
async def outbound_call(req: OutboundCallRequest):
    """Trigger an outbound call via Twilio REST API."""
    load_agent_config(req.agent_name)
    call_id, call_sid = place_outbound_call(
        _pending_calls,
        agent_name=req.agent_name,
        target_number=req.target_number,
        case_data=req.case_data,
        from_number=req.from_number,
    )
    logger.info(f"Outbound initiated call_id={call_id} sid={call_sid}")
    return OutboundCallResponse(
        call_sid=call_sid,
        call_id=call_id,
        status="call_initiated",
    )


# ═════════════════════════════════════════════════════════════════════════════
# CALLING ENGINE: Twilio inbound + WebSocket
# ═════════════════════════════════════════════════════════════════════════════


_SKIP_TWILIO_VALIDATION = os.getenv("SKIP_TWILIO_VALIDATION", "false").lower() == "true"


# Twilio → our terminal status mapping.
# Non-terminal states (queued, initiated, ringing, in-progress) are ignored —
# the pipeline's own write wins when the call connects and completes normally.
_TWILIO_TERMINAL_MAP = {
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "canceled": "failed",
    "failed": "failed",
}


async def _validate_twilio_signature(request: Request, form_data: Any) -> bool:
    """Return True if the Twilio signature is valid (or validation is skipped)."""
    if _SKIP_TWILIO_VALIDATION:
        return True
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not auth_token:
        return False
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    params = {k: str(v) for k, v in form_data.items()}
    return RequestValidator(auth_token).validate(url, params, signature)


@app.post("/api/twilio/status-callback")
async def twilio_status_callback(request: Request):
    """Receive Twilio call status updates.

    On terminal status (busy, no-answer, canceled, failed, completed), write
    the corresponding status to the batch row or call request so the consumer
    can release its concurrency slot within seconds. For `completed`, only
    write if the pipeline hasn't already (pipeline's value wins).
    """
    form_data = await request.form()

    if not await _validate_twilio_signature(request, form_data):
        logger.warning(
            "Invalid Twilio signature on status callback "
            f"from {request.client.host if request.client else 'unknown'}"
        )
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    params = {k: str(v) for k, v in form_data.items()}
    call_sid = params.get("CallSid", "")
    call_status = params.get("CallStatus", "")

    if not call_sid or not call_status:
        return {"status": "ignored"}

    our_status = _TWILIO_TERMINAL_MAP.get(call_status)
    if not our_status:
        # Non-terminal: queued / initiated / ringing / in-progress. Nothing to do.
        logger.debug(f"Twilio status callback {call_sid}: {call_status} (non-terminal)")
        return {"status": "noted", "twilio_status": call_status}

    from server.twilio_outbound import get_pending_call_by_sid

    found = get_pending_call_by_sid(_pending_calls, call_sid)
    if not found:
        logger.info(f"Twilio status callback for unknown sid {call_sid} ({call_status})")
        return {"status": "unknown_call", "twilio_status": call_status}

    _our_call_id, entry = found
    batch_row_id = entry.get("batch_row_id")
    request_id = entry.get("request_id")

    err_msg = None if our_status == "completed" else f"Twilio: {call_status}"

    try:
        if batch_row_id:
            # Don't overwrite if pipeline already set it to completed with full data.
            try:
                existing = await invoke("GET", f"/batch-rows/{batch_row_id}")
            except Exception:
                existing = None
            existing_status = existing.get("status") if isinstance(existing, dict) else None
            if existing_status == "completed":
                logger.debug(
                    f"Status callback: batch_row {batch_row_id} already completed, skipping"
                )
            else:
                body = {"status": our_status}
                if err_msg:
                    body["error_message"] = err_msg
                await invoke("PUT", f"/batch-rows/{batch_row_id}", body=body)
                logger.info(
                    f"Status callback: batch_row {batch_row_id} → {our_status} "
                    f"(twilio={call_status})"
                )

        if request_id:
            try:
                existing = await invoke("GET", f"/call-requests/{request_id}")
            except Exception:
                existing = None
            existing_status = existing.get("status") if isinstance(existing, dict) else None
            if existing_status == "completed":
                logger.debug(
                    f"Status callback: call_request {request_id} already completed, skipping"
                )
            else:
                body = {"status": our_status}
                if err_msg:
                    body["error_message"] = err_msg
                await invoke("PUT", f"/call-requests/{request_id}", body=body)
                logger.info(
                    f"Status callback: call_request {request_id} → {our_status} "
                    f"(twilio={call_status})"
                )
    except Exception:
        logger.exception("Twilio status callback write failed")

    return {"status": "updated", "our_status": our_status, "twilio_status": call_status}


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Return TwiML for inbound calls — routes via phone_numbers from Lambda."""
    if not _SKIP_TWILIO_VALIDATION:
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        if not auth_token:
            logger.error("TWILIO_AUTH_TOKEN not set — cannot validate webhook")
            raise HTTPException(status_code=500, detail="Server misconfigured")

        signature = request.headers.get("X-Twilio-Signature", "")
        url = str(request.url)
        form_data = await request.form()
        params = {k: str(v) for k, v in form_data.items()}

        validator = RequestValidator(auth_token)
        if not validator.validate(url, params, signature):
            logger.warning(
                "Invalid Twilio signature on incoming webhook "
                f"from {request.client.host if request.client else 'unknown'}"
            )
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

        form = form_data
    else:
        form = await request.form()
    to_number = str(form.get("To", ""))

    agent_name = None
    if to_number:
        try:
            phones = await list_phone_numbers()
            for p in phones:
                if p.get("number") == to_number:
                    inbound = p.get("inbound_agent")
                    if isinstance(inbound, dict) and inbound.get("name"):
                        agent_name = inbound["name"]
                    break
        except Exception:
            logger.exception("Failed to look up inbound agent from Lambda")

    if not agent_name:
        agent_name = os.getenv("DEFAULT_INBOUND_AGENT", "chris")

    ws_url = ws_url_from_env()
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    stream.parameter(name="agent_name", value=agent_name)
    connect.append(stream)
    response.append(connect)
    response.pause(length=60)

    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Accept Twilio Media Stream WebSocket and run the voice agent pipeline."""
    await websocket.accept()
    logger.info("Twilio WebSocket connection accepted")

    try:
        await handle_twilio_websocket(websocket, _pending_calls)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# CALLING ENGINE: WebRTC test calls
# ═════════════════════════════════════════════════════════════════════════════

_test_call_sessions: dict[str, dict] = {}


@app.api_route("/api/test-call/connect", methods=["POST", "PATCH"])
async def test_call_connect(request: Request):
    body = await request.json()
    logger.info(f"[TEST CALL] {request.method} body_keys={list(body.keys())} qp={dict(request.query_params)}")

    # ── PATCH → ICE trickle candidates
    if request.method == "PATCH":
        candidates = [
            IceCandidate(
                candidate=c.get("candidate", ""),
                sdp_mid=c.get("sdpMid", c.get("sdp_mid", "")),
                sdp_mline_index=c.get("sdpMLineIndex", c.get("sdp_mline_index", 0)),
            )
            for c in body.get("candidates", [])
        ]
        patch_req = SmallWebRTCPatchRequest(
            pc_id=body.get("pc_id", body.get("pcId", "")),
            candidates=candidates,
        )
        await _webrtc_handler.handle_patch_request(patch_req)
        return {"status": "ok"}

    # ── POST with SDP → WebRTC offer/answer exchange
    if body.get("sdp"):
        req_data = body.get("requestData") or body.get("request_data") or {}
        qp = request.query_params
        agent_name = (
            req_data.get("agent_name")
            or body.get("agent_name")
            or qp.get("agent_name")
        )
        use_draft_raw = (
            req_data.get("use_draft")
            if req_data.get("use_draft") is not None
            else body.get("use_draft")
            if body.get("use_draft") is not None
            else qp.get("use_draft")
        )
        use_draft = use_draft_raw not in (False, "false", "0") if use_draft_raw is not None else True

        case_data = req_data.get("case_data") or body.get("case_data") or {}
        if isinstance(case_data, str):
            try:
                case_data = _json.loads(case_data)
            except ValueError:
                case_data = {}

        if not agent_name and _test_call_sessions:
            last = list(_test_call_sessions.values())[-1]
            agent_name = last["agent_name"]
            use_draft = last["use_draft"]
            case_data = case_data or last.get("case_data", {})

        logger.info(f"[TEST CALL SDP] agent={agent_name} case_data={case_data}")

        if not agent_name:
            raise HTTPException(
                status_code=400,
                detail="agent_name required in requestData, body, query params, or a prior connect call",
            )

        sdp_request = SmallWebRTCRequest(
            sdp=body["sdp"],
            type=body.get("type", "offer"),
            pc_id=body.get("pc_id", body.get("pcId")),
            restart_pc=body.get("restart_pc", body.get("restartPc")),
            request_data=req_data,
        )

        async def on_connection(connection):
            from server.webrtc_handler import run_test_call

            asyncio.create_task(run_test_call(connection, agent_name, use_draft, case_data))

        answer = await _webrtc_handler.handle_web_request(
            request=sdp_request,
            webrtc_connection_callback=on_connection,
        )
        return answer

    # ── POST without SDP → RTVI connect
    config = body.get("body", body)
    qp = request.query_params
    agent_name = config.get("agent_name") or qp.get("agent_name")
    use_draft_raw = (
        config.get("use_draft")
        if config.get("use_draft") is not None
        else qp.get("use_draft")
    )
    use_draft = use_draft_raw not in (False, "false", "0") if use_draft_raw is not None else True

    case_data = config.get("case_data") or {}
    if isinstance(case_data, str):
        try:
            case_data = _json.loads(case_data)
        except ValueError:
            case_data = {}
    qp_case = qp.get("case_data")
    if qp_case and not case_data:
        try:
            case_data = _json.loads(qp_case)
        except ValueError:
            case_data = {}

    if not agent_name:
        raise HTTPException(status_code=400, detail="agent_name is required")

    if use_draft:
        check = await get_agent_draft(agent_name)
    else:
        check = await get_agent_config(agent_name)

    if not check:
        raise HTTPException(
            status_code=404,
            detail=f"{'Draft' if use_draft else 'Agent'} not found: {agent_name}",
        )

    session_id = str(uuid.uuid4())
    _test_call_sessions[session_id] = {
        "agent_name": agent_name,
        "use_draft": use_draft,
        "case_data": case_data,
    }
    logger.info(f"[TEST CALL CONNECT] session={session_id} agent={agent_name} case_data={case_data}")

    return {
        "sessionId": session_id,
        "iceConfig": {
            "iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}],
        },
    }


try:
    from pipecat_ai_small_webrtc_prebuilt import SmallWebRTCPrebuiltUI

    app.mount("/test-call", SmallWebRTCPrebuiltUI, name="test-call-ui")
    logger.info("Mounted prebuilt WebRTC test UI at /test-call")
except Exception:
    logger.debug("Prebuilt WebRTC UI not available")
