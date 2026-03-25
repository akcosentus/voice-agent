"""FastAPI server — Twilio WebSocket telephony for Cosentus voice agents."""

import asyncio
import os
import uuid

from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import FastAPI, File, Form, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from loguru import logger
from pydantic import BaseModel
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from core.call_store import get_batch, insert_batch
from core.config_loader import load_agent_config
from server.batch_parse import build_row_payloads, parse_upload_file
from server.batch_runner import run_batch_dial
from server.twilio_outbound import place_outbound_call, ws_url_from_env
from server.ws_handler import handle_twilio_websocket

app = FastAPI(title="Cosentus Voice Agent Server")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "https://voice-agents-front-zeta.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_pending_calls: dict[str, dict] = {}


# ── Models ──────────────────────────────────────────────────────────────────


class OutboundCallRequest(BaseModel):
    agent_name: str
    target_number: str
    case_data: dict


class OutboundCallResponse(BaseModel):
    call_sid: str
    call_id: str
    status: str


# ── Helpers ─────────────────────────────────────────────────────────────────


def _storage_upload_batch_file(path: str, data: bytes, content_type: str) -> None:
    from core.supabase_client import get_supabase

    get_supabase().storage.from_("batch-files").upload(
        path,
        data,
        file_options={"content-type": content_type, "upsert": "true"},
    )


def _storage_download(path: str) -> bytes:
    from core.supabase_client import get_supabase

    return get_supabase().storage.from_("batch-files").download(path)


# ── Endpoints ───────────────────────────────────────────────────────────────


@app.get("/api/agents")
async def list_agents():
    """List available agents by scanning the agents/ directory."""
    agents_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agents")
    result = []
    if not os.path.isdir(agents_dir):
        return JSONResponse(content={"agents": result})

    for category in sorted(os.listdir(agents_dir)):
        cat_path = os.path.join(agents_dir, category)
        if not os.path.isdir(cat_path) or category.startswith(("_", ".")):
            continue
        for variant in sorted(os.listdir(cat_path)):
            var_path = os.path.join(cat_path, variant)
            config_path = os.path.join(var_path, "config.yaml")
            if os.path.isdir(var_path) and os.path.isfile(config_path):
                result.append(f"{category}/{variant}")

    return JSONResponse(content={"agents": result})


@app.post("/api/calls/outbound", response_model=OutboundCallResponse)
async def outbound_call(req: OutboundCallRequest):
    """Trigger an outbound call via Twilio REST API."""
    config = load_agent_config(req.agent_name)
    call_id, call_sid = place_outbound_call(
        _pending_calls,
        agent_name=req.agent_name,
        target_number=req.target_number,
        case_data=req.case_data,
        from_number=config.telephony.phone_number,
    )
    logger.info(f"Outbound initiated call_id={call_id} sid={call_sid}")
    return OutboundCallResponse(
        call_sid=call_sid,
        call_id=call_id,
        status="call_initiated",
    )


@app.post("/api/batches/upload")
async def upload_batch(
    file: UploadFile = File(...),
    agent_name: str = Form(...),
):
    """Upload Excel/CSV for batch validation and storage."""
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
        return JSONResponse(
            status_code=503,
            content={"detail": "Supabase is not configured"},
        )
    raw = await file.read()
    fname = file.filename or "upload.xlsx"
    try:
        headers, rows = parse_upload_file(raw, fname)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})

    phone_idx, payloads, summary = build_row_payloads(headers, rows)
    batch_id = str(uuid.uuid4())
    ext = os.path.splitext(fname)[1] or ".xlsx"
    storage_path = f"batch-files/{batch_id}/input{ext}"
    ct = file.content_type or "application/octet-stream"
    await asyncio.to_thread(_storage_upload_batch_file, storage_path, raw, ct)

    config = load_agent_config(agent_name)
    from_number = config.telephony.phone_number
    row = {
        "id": batch_id,
        "name": fname,
        "agent_name": agent_name,
        "from_number": from_number,
        "status": "ready",
        "total_rows": summary["valid"],
        "completed_rows": 0,
        "failed_rows": 0,
        "rows": payloads,
        "input_file_path": storage_path,
    }
    await insert_batch(row)

    return {
        "batch_id": batch_id,
        "columns": headers,
        "phone_column_index": phone_idx,
        "summary": summary,
        "rows": [
            {
                "row_index": p["row_index"],
                "validation": p["validation"],
                "phone_e164": p["phone_e164"],
            }
            for p in payloads
        ],
    }


@app.post("/api/batches/{batch_id}/start")
async def start_batch(batch_id: str, concurrency: int = 1):
    """Start dialing all valid rows in a batch (background)."""
    batch = await get_batch(batch_id)
    if not batch:
        return JSONResponse(status_code=404, content={"detail": "batch not found"})
    if batch.get("status") != "ready":
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"batch status is {batch.get('status')!r}, expected 'ready'",
            },
        )
    asyncio.create_task(run_batch_dial(_pending_calls, batch_id, concurrency))
    return {
        "status": "running",
        "total_calls": batch.get("total_rows", 0),
    }


@app.get("/api/batches/{batch_id}/status")
async def batch_status(batch_id: str):
    batch = await get_batch(batch_id)
    if not batch:
        return JSONResponse(status_code=404, content={"detail": "batch not found"})
    return {
        "batch_id": batch_id,
        "status": batch.get("status"),
        "total": batch.get("total_rows", 0),
        "completed": batch.get("completed_rows", 0),
        "failed": batch.get("failed_rows", 0),
    }


@app.get("/api/batches/{batch_id}/results")
async def batch_results(batch_id: str):
    batch = await get_batch(batch_id)
    if not batch:
        return JSONResponse(status_code=404, content={"detail": "batch not found"})
    out_path = batch.get("output_file_path")
    if not out_path:
        return JSONResponse(
            status_code=404,
            content={"detail": "results not ready yet"},
        )
    try:
        data = await asyncio.to_thread(_storage_download, out_path)
    except Exception as e:
        logger.exception("Download batch results failed")
        return JSONResponse(status_code=500, content={"detail": str(e)})
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="batch_{batch_id}_results.xlsx"',
        },
    )


@app.post("/twilio/incoming")
async def twilio_incoming():
    """Return TwiML for inbound calls — connects to the WebSocket stream."""
    ws_url = ws_url_from_env()
    default_agent = os.getenv("DEFAULT_INBOUND_AGENT", "chris/claim_status")

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    stream.parameter(name="agent_name", value=default_agent)
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
