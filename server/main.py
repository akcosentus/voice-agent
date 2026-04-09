"""FastAPI server — Twilio WebSocket + WebRTC telephony for Cosentus voice agents."""

import asyncio
import os
import re
import uuid

from dotenv import load_dotenv

load_dotenv(override=True)

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from loguru import logger
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
    SmallWebRTCPatchRequest,
    IceCandidate,
)

from core.call_store import get_batch, insert_batch, update_batch
from core.config_loader import invalidate_cache, load_agent_config
from core.supabase_client import get_supabase
from core.validation import (
    ALLOWED_LLM_MODELS,
    ALLOWED_STT_LANGUAGES,
    ALLOWED_STT_PROVIDERS,
    ALLOWED_TOOL_TYPES,
    ALLOWED_TTS_MODELS,
    ALLOWED_TTS_PROVIDERS,
    ALLOWED_VOICEMAIL_ACTIONS,
    E164_RE,
    FIELD_LABELS,
    FIELD_RANGES,
    NEW_AGENT_DEFAULTS,
    TOOL_SETTINGS_SCHEMA,
    validate_agent_data,
)
from server.batch_parse import build_row_payloads, normalize_phone, parse_upload_file
from server.batch_runner import MAX_SERVER_CONCURRENCY, run_batch
from server.schemas import (
    BatchControlResponse,
    CloneAgentRequest,
    CreateAgentRequest,
    CreatePhoneNumberRequest,
    OutboundCallRequest,
    OutboundCallResponse,
    PurchasePhoneNumberRequest,
    StartBatchRequest,
    StartBatchResponse,
    UpdateAgentRequest,
    UpdateBatchRowsRequest,
    UpdateBatchRowsResponse,
    UpdatePhoneNumberRequest,
    UpdatePromptRequest,
    UploadBatchResponse,
    UploadRowResponse,
    VoiceAddRequest,
    VoiceLookupRequest,
)
from server.twilio_outbound import place_outbound_call, ws_url_from_env
from server.ws_handler import handle_twilio_websocket

app = FastAPI(title="Cosentus Voice Agent Server")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://voice-agents-front-zeta.vercel.app",
        "https://unprofessed-unephemeral-dwana.ngrok-free.dev",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_pending_calls: dict[str, dict] = {}
_webrtc_handler = SmallWebRTCRequestHandler()


@app.on_event("startup")
async def _recover_orphaned_batches():
    """Resume batches that were running/scheduled when the server last stopped."""
    try:
        result = (
            get_supabase()
            .table("batches")
            .select("id")
            .in_("status", ["running", "scheduled"])
            .execute()
        )
        for b in result.data or []:
            bid = b["id"]
            logger.info(f"Recovering orphaned batch: {bid}")
            asyncio.create_task(run_batch(_pending_calls, bid))
    except Exception:
        logger.exception("Failed to recover orphaned batches on startup")



# ── Helpers ─────────────────────────────────────────────────────────────────



def _storage_upload_batch_file(path: str, data: bytes, content_type: str) -> None:
    get_supabase().storage.from_("batch-files").upload(
        path,
        data,
        file_options={"content-type": content_type, "upsert": "true"},
    )


def _storage_download(path: str) -> bytes:
    return get_supabase().storage.from_("batch-files").download(path)


def _get_agent_row(agent_name: str) -> dict:
    resp = (
        get_supabase()
        .table("agents")
        .select("*")
        .eq("name", agent_name)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_name}")
    return resp.data[0]


def _agent_detail_from_row(row: dict) -> dict:
    prompt = row.get("system_prompt", "")
    variables = sorted(set(re.findall(r"\{\{(\w+)\}\}", prompt)))
    pca = row.get("post_call_analyses") or {"model": "claude-haiku-4-5-20251001", "fields": []}
    tools = row.get("tools") or []
    return {
        "id": row["id"],
        "name": row["name"],
        "display_name": row.get("display_name", ""),
        "description": row.get("description", ""),
        "llm": {
            "provider": row.get("llm_provider", "anthropic"),
            "model": row.get("llm_model", "claude-sonnet-4-6"),
            "temperature": row.get("temperature", 0.7),
            "max_tokens": row.get("max_tokens", 200),
            "enable_prompt_caching": row.get("enable_prompt_caching", True),
        },
        "tts": {
            "provider": row.get("tts_provider", "elevenlabs"),
            "voice_id": row.get("tts_voice_id", ""),
            "model": row.get("tts_model", "eleven_turbo_v2_5"),
            "settings": {
                "stability": row.get("tts_stability"),
                "similarity_boost": row.get("tts_similarity_boost"),
                "style": row.get("tts_style"),
                "use_speaker_boost": row.get("tts_use_speaker_boost"),
                "speed": row.get("tts_speed"),
            },
        },
        "stt": {
            "provider": row.get("stt_provider", "deepgram"),
            "language": row.get("stt_language", "en"),
            "keywords": list(row.get("stt_keywords") or []),
        },
        "tools": tools,
        "first_message": row.get("first_message", ""),
        "prompt_variables": variables,
        "prompt_preview": prompt[:500] + ("..." if len(prompt) > 500 else ""),
        "recording": {
            "enabled": row.get("recording_enabled", True),
            "channels": row.get("recording_channels", 2),
        },
        "post_call_analyses": pca,
        "idle_timeout_secs": row.get("idle_timeout_secs", 30),
        "idle_message": row.get("idle_message", ""),
        "max_call_duration_secs": row.get("max_call_duration_secs", 1800),
        "voicemail_action": row.get("voicemail_action", "hangup"),
        "voicemail_message": row.get("voicemail_message", ""),
        "max_retries": row.get("max_retries", 0),
        "retry_delay_secs": row.get("retry_delay_secs", 300),
        "default_concurrency": row.get("default_concurrency", 1),
        "calling_window_start": row.get("calling_window_start"),
        "calling_window_end": row.get("calling_window_end"),
        "calling_window_days": row.get("calling_window_days"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _record_agent_version(agent_id: str, change_type: str, old_val: dict, new_val: dict):
    try:
        get_supabase().table("agent_versions").insert({
            "agent_id": agent_id,
            "change_type": change_type,
            "previous_value": old_val,
            "new_value": new_val,
        }).execute()
    except Exception:
        logger.exception("Failed to record agent version")


# ── Agent schema endpoint ───────────────────────────────────────────────────


@app.get("/api/agent-schema")
async def get_agent_schema():
    """Return allowed values and defaults for frontend form rendering."""
    return {
        "llm_models": ALLOWED_LLM_MODELS,
        "tts_providers": ALLOWED_TTS_PROVIDERS,
        "tts_models": ALLOWED_TTS_MODELS,
        "stt_providers": ALLOWED_STT_PROVIDERS,
        "stt_languages": ALLOWED_STT_LANGUAGES,
        "tool_types": ALLOWED_TOOL_TYPES,
        "tool_settings_schema": TOOL_SETTINGS_SCHEMA,
        "voicemail_actions": ALLOWED_VOICEMAIL_ACTIONS,
        "field_ranges": FIELD_RANGES,
        "defaults": NEW_AGENT_DEFAULTS,
        "field_labels": FIELD_LABELS,
        "post_call_field_types": ["text", "selector"],
        "post_call_default": {
            "model": "claude-haiku-4-5-20251001",
            "fields": [],
        },
    }


# ── Agent CRUD ──────────────────────────────────────────────────────────────


@app.get("/api/agents")
async def list_agents():
    """List available agents from Supabase."""
    resp = (
        get_supabase()
        .table("agents")
        .select("id, name, display_name, description, llm_model, tts_model, tts_voice_id")
        .eq("is_active", True)
        .order("name")
        .execute()
    )
    return {"agents": resp.data or []}


@app.post("/api/agents")
async def create_agent(payload: CreateAgentRequest):
    """Create a new agent with defaults filled in."""
    cleaned, errors = validate_agent_data(
        {"name": payload.name, "display_name": payload.display_name or payload.name},
        is_create=True,
    )
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    existing = (
        get_supabase()
        .table("agents")
        .select("id")
        .eq("name", cleaned["name"])
        .limit(1)
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail=f"Agent '{cleaned['name']}' already exists")

    resp = get_supabase().table("agents").insert(cleaned).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to create agent")
    new_row = resp.data[0]

    skip_drafts = {"id", "created_at", "updated_at", "created_by", "extends_agent_id",
                   "overrides", "is_active"}
    draft_data = {k: v for k, v in new_row.items() if k not in skip_drafts}
    draft_data["agent_id"] = new_row["id"]
    draft_data["has_unpublished_changes"] = False
    try:
        get_supabase().table("agent_drafts").upsert(
            draft_data, on_conflict="agent_id"
        ).execute()
    except Exception:
        logger.warning(f"Draft creation failed for {cleaned.get('name')}, editor will handle it")

    return _agent_detail_from_row(new_row)


@app.get("/api/agents/{agent_name:path}/prompt")
async def get_agent_prompt(agent_name: str):
    """Return the full system prompt text for an agent."""
    row = _get_agent_row(agent_name)
    content = row.get("system_prompt", "")
    variables = sorted(set(re.findall(r"\{\{(\w+)\}\}", content)))
    return {"content": content, "prompt_variables": variables}


@app.put("/api/agents/{agent_name:path}/prompt")
async def update_agent_prompt(agent_name: str, payload: UpdatePromptRequest):
    """Update the agent's system prompt in Supabase."""
    row = _get_agent_row(agent_name)
    content = payload.content
    old_prompt = row.get("system_prompt", "")
    get_supabase().table("agents").update({"system_prompt": content}).eq("id", row["id"]).execute()
    invalidate_cache(agent_name)
    _record_agent_version(row["id"], "prompt", {"system_prompt": old_prompt}, {"system_prompt": content})

    variables = sorted(set(re.findall(r"\{\{(\w+)\}\}", content)))
    return {
        "prompt_variables": variables,
        "prompt_preview": content[:500] + ("..." if len(content) > 500 else ""),
    }


@app.post("/api/agents/{agent_name:path}/clone")
async def clone_agent(agent_name: str, payload: CloneAgentRequest):
    """Clone an existing agent with a new name and a matching draft row."""
    row = _get_agent_row(agent_name)

    source_name = row["name"]
    source_display = row.get("display_name", source_name)
    new_name = payload.name or f"{source_name}_clone"
    new_display = payload.display_name or f"{source_display} (Clone)"

    existing = get_supabase().table("agents").select("id").eq("name", new_name).limit(1).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail=f"Agent '{new_name}' already exists")

    skip_agents = {"id", "created_at", "updated_at", "created_by", "extends_agent_id", "overrides"}
    clone_data = {k: v for k, v in row.items() if k not in skip_agents}
    clone_data["name"] = new_name
    clone_data["display_name"] = new_display
    clone_data["is_active"] = True

    resp = get_supabase().table("agents").insert(clone_data).execute()
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to clone agent")
    new_row = resp.data[0]

    skip_drafts = {"id", "created_at", "updated_at", "created_by", "extends_agent_id",
                   "overrides", "is_active"}
    draft_data = {k: v for k, v in new_row.items() if k not in skip_drafts}
    draft_data["agent_id"] = new_row["id"]
    draft_data["has_unpublished_changes"] = False
    try:
        get_supabase().table("agent_drafts").insert(draft_data).execute()
    except Exception:
        logger.exception("Failed to create draft for cloned agent")

    invalidate_cache(new_name)
    return _agent_detail_from_row(new_row)


@app.put("/api/agents/{agent_name:path}")
async def update_agent(agent_name: str, payload: UpdateAgentRequest):
    """Partial update of an agent's config in Supabase."""
    row = _get_agent_row(agent_name)
    body = payload.model_dump(exclude_none=True)

    cleaned, errors = validate_agent_data(body, is_create=False)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    if not cleaned:
        return _agent_detail_from_row(row)

    snapshot_before = {k: row.get(k) for k in cleaned}
    try:
        get_supabase().table("agents").update(cleaned).eq("id", row["id"]).execute()
    except Exception as e:
        logger.exception("Agent update failed")
        raise HTTPException(status_code=500, detail=f"Database update failed: {e}")
    invalidate_cache(agent_name)
    _record_agent_version(row["id"], "config", snapshot_before, cleaned)

    try:
        get_supabase().table("agent_drafts").update(
            {**cleaned, "has_unpublished_changes": False}
        ).eq("agent_id", row["id"]).execute()
    except Exception as e:
        logger.warning(f"Failed to sync draft after publish for {agent_name}: {e}")

    updated_row = _get_agent_row(agent_name)
    return _agent_detail_from_row(updated_row)


@app.delete("/api/agents/{agent_name:path}")
async def delete_agent(agent_name: str):
    """Soft delete an agent."""
    row = _get_agent_row(agent_name)
    get_supabase().table("agents").update({"is_active": False}).eq("id", row["id"]).execute()
    invalidate_cache(agent_name)
    return {"status": "deleted", "name": agent_name}


@app.get("/api/agents/{agent_name:path}/versions")
async def list_agent_versions(agent_name: str):
    """Return all version history entries for an agent, newest first."""
    row = _get_agent_row(agent_name)
    resp = (
        get_supabase()
        .table("agent_versions")
        .select("*")
        .eq("agent_id", row["id"])
        .order("created_at", desc=True)
        .execute()
    )
    return {"agent_name": agent_name, "versions": resp.data or []}


@app.get("/api/agents/{agent_name:path}/versions/{version_id}")
async def get_agent_version(agent_name: str, version_id: str):
    """Return a specific version entry by its row ID."""
    row = _get_agent_row(agent_name)
    resp = (
        get_supabase()
        .table("agent_versions")
        .select("*")
        .eq("id", version_id)
        .eq("agent_id", row["id"])
        .limit(1)
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail=f"Version {version_id} not found")
    return resp.data[0]


@app.get("/api/agents/{agent_name:path}")
async def get_agent(agent_name: str):
    """Return full config for a single agent."""
    row = _get_agent_row(agent_name)
    return _agent_detail_from_row(row)


# ── Phone number CRUD ───────────────────────────────────────────────────────


@app.get("/api/phone-numbers")
async def list_phone_numbers():
    """List all active phone numbers with joined agent info."""
    resp = get_supabase().table("phone_numbers").select("*").eq("is_active", True).order("number").execute()
    rows = resp.data or []

    agent_ids = set()
    for r in rows:
        if r.get("inbound_agent_id"):
            agent_ids.add(r["inbound_agent_id"])
        if r.get("outbound_agent_id"):
            agent_ids.add(r["outbound_agent_id"])

    agents_map: dict[str, dict] = {}
    if agent_ids:
        agent_resp = (
            get_supabase()
            .table("agents")
            .select("id, name, display_name")
            .in_("id", list(agent_ids))
            .execute()
        )
        for a in (agent_resp.data or []):
            agents_map[a["id"]] = {"id": a["id"], "name": a["name"], "display_name": a["display_name"]}

    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "number": r["number"],
            "friendly_name": r.get("friendly_name", ""),
            "inbound_agent": agents_map.get(r.get("inbound_agent_id")),
            "outbound_agent": agents_map.get(r.get("outbound_agent_id")),
            "is_active": r.get("is_active", True),
            "created_at": r.get("created_at"),
            "updated_at": r.get("updated_at"),
        })
    return {"phone_numbers": result}


@app.post("/api/phone-numbers")
async def create_phone_number(payload: CreatePhoneNumberRequest):
    """Add a new phone number."""
    existing = get_supabase().table("phone_numbers").select("id").eq("number", payload.number).limit(1).execute()
    if existing.data:
        raise HTTPException(status_code=409, detail=f"Phone number {payload.number} already exists")

    for field_name in ("inbound_agent_id", "outbound_agent_id"):
        aid = getattr(payload, field_name, None)
        if aid:
            check = get_supabase().table("agents").select("id").eq("id", aid).eq("is_active", True).limit(1).execute()
            if not check.data:
                label = field_name.replace("_", " ").replace(" id", "")
                raise HTTPException(status_code=400, detail=f"{label} '{aid}' not found or inactive")

    row = {
        "number": payload.number,
        "friendly_name": payload.friendly_name,
        "inbound_agent_id": payload.inbound_agent_id,
        "outbound_agent_id": payload.outbound_agent_id,
    }
    try:
        resp = get_supabase().table("phone_numbers").insert(row).execute()
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"Phone number {payload.number} already exists")
        raise
    if not resp.data:
        raise HTTPException(status_code=500, detail="Failed to create phone number")
    return resp.data[0]


@app.put("/api/phone-numbers/{phone_id}")
async def update_phone_number(phone_id: str, payload: UpdatePhoneNumberRequest):
    """Update a phone number's friendly name or agent assignments."""
    cols = payload.model_dump(exclude_none=True)
    if not cols:
        raise HTTPException(status_code=400, detail="No fields to update")

    for field_name in ("inbound_agent_id", "outbound_agent_id"):
        aid = cols.get(field_name)
        if aid is not None:
            check = get_supabase().table("agents").select("id").eq("id", aid).eq("is_active", True).limit(1).execute()
            if not check.data:
                label = field_name.replace("_", " ").replace(" id", "")
                raise HTTPException(status_code=400, detail=f"{label} {aid} not found or inactive")

    resp = get_supabase().table("phone_numbers").update(cols).eq("id", phone_id).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Phone number not found")
    return resp.data[0]


@app.delete("/api/phone-numbers/{phone_id}")
async def delete_phone_number(phone_id: str):
    """Soft delete a phone number."""
    resp = (
        get_supabase()
        .table("phone_numbers")
        .update({"is_active": False})
        .eq("id", phone_id)
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail="Phone number not found")
    return {"status": "deleted", "id": phone_id}


@app.post("/api/phone-numbers/sync-twilio")
async def sync_twilio_numbers():
    """Fetch all phone numbers from Twilio and upsert into phone_numbers table."""
    from twilio.rest import Client

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")

    client = Client(account_sid, auth_token)
    supabase = get_supabase()

    synced = []
    for number in client.incoming_phone_numbers.stream():
        existing = (
            supabase.table("phone_numbers")
            .select("id, friendly_name")
            .eq("number", number.phone_number)
            .limit(1)
            .execute()
        )
        if existing.data:
            existing_name = existing.data[0].get("friendly_name", "")
            if not existing_name or existing_name == number.phone_number:
                supabase.table("phone_numbers").update({
                    "friendly_name": number.friendly_name or "",
                }).eq("id", existing.data[0]["id"]).execute()
            synced.append({"number": number.phone_number, "status": "exists"})
        else:
            supabase.table("phone_numbers").insert({
                "number": number.phone_number,
                "friendly_name": number.friendly_name or "",
                "is_active": True,
            }).execute()
            synced.append({"number": number.phone_number, "status": "added"})

    return {"synced": synced, "total": len(synced)}


@app.get("/api/phone-numbers/search")
async def search_available_numbers(
    country: str = "US",
    area_code: str | None = None,
    contains: str | None = None,
    limit: int = 20,
):
    """Search Twilio's available phone number inventory."""
    from twilio.rest import Client

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")

    client = Client(account_sid, auth_token)

    kwargs: dict = {"limit": min(limit, 30)}
    if area_code:
        kwargs["area_code"] = area_code
    if contains:
        kwargs["contains"] = contains

    numbers = client.available_phone_numbers(country).local.list(**kwargs)

    return {
        "numbers": [
            {
                "number": n.phone_number,
                "friendly_name": n.friendly_name,
                "locality": n.locality,
                "region": n.region,
                "capabilities": {
                    "voice": n.capabilities.get("voice", False),
                    "sms": n.capabilities.get("SMS", False),
                    "mms": n.capabilities.get("MMS", False),
                },
            }
            for n in numbers
        ],
        "country": country,
        "count": len(numbers),
    }


@app.post("/api/phone-numbers/purchase")
async def purchase_number(payload: PurchasePhoneNumberRequest):
    """Purchase a number from Twilio and add it to our phone_numbers table."""
    phone_number = payload.number
    friendly_name = payload.friendly_name

    supabase = get_supabase()
    existing = supabase.table("phone_numbers") \
        .select("*").eq("number", phone_number).limit(1).execute()
    if existing.data:
        return {
            "status": "already_owned",
            "number": existing.data[0]["number"],
            "phone_number_record": existing.data[0],
        }

    from twilio.rest import Client

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")

    client = Client(account_sid, auth_token)

    try:
        purchased = client.incoming_phone_numbers.create(
            phone_number=phone_number,
            friendly_name=friendly_name or phone_number,
        )

        result = supabase.table("phone_numbers").insert({
            "number": purchased.phone_number,
            "friendly_name": friendly_name or purchased.friendly_name or "",
            "is_active": True,
        }).execute()

        return {
            "status": "purchased",
            "number": purchased.phone_number,
            "sid": purchased.sid,
            "monthly_cost": "$2.00",
            "phone_number_record": result.data[0] if result.data else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Purchase failed: {e}")


@app.post("/api/phone-numbers/{phone_number_id}/release")
async def release_number(phone_number_id: str):
    """Release a number back to Twilio and remove from our system."""
    supabase = get_supabase()

    resp = supabase.table("phone_numbers").select("*").eq("id", phone_number_id).limit(1).execute()
    if not resp.data:
        raise HTTPException(status_code=404, detail="Phone number not found")

    number = resp.data[0]["number"]

    from twilio.rest import Client

    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")

    client = Client(account_sid, auth_token)

    try:
        twilio_numbers = client.incoming_phone_numbers.list(phone_number=number)
        if twilio_numbers:
            twilio_numbers[0].delete()

        supabase.table("phone_numbers").delete().eq("id", phone_number_id).execute()

        return {"status": "released", "number": number}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Release failed: {e}")


# ── Voice library ────────────────────────────────────────────────────────────


@app.get("/api/voices")
async def list_voices():
    """List all voices in the voice library."""
    result = get_supabase().table("voice_library") \
        .select("*") \
        .eq("is_active", True) \
        .order("name") \
        .execute()
    return {"voices": result.data or []}


@app.get("/api/voices/{voice_id}")
async def get_voice(voice_id: str):
    """Get a single voice from the library."""
    result = get_supabase().table("voice_library") \
        .select("*") \
        .eq("voice_id", voice_id) \
        .eq("is_active", True) \
        .limit(1) \
        .execute()
    if not result.data:
        raise HTTPException(status_code=404, detail=f"Voice not found: {voice_id}")
    return result.data[0]


@app.post("/api/voices/lookup")
async def lookup_voice(payload: VoiceLookupRequest):
    """Look up a voice ID on ElevenLabs without adding to library."""
    voice_id = payload.voice_id

    api_key = os.getenv("ELEVENLABS_API_KEY")
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.elevenlabs.io/v1/voices/{voice_id}",
            headers={"xi-api-key": api_key},
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Voice not found on ElevenLabs")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ElevenLabs API error: {resp.status_code}")

    data = resp.json()
    labels = data.get("labels", {})
    return {
        "voice_id": data["voice_id"],
        "name": data.get("name", "Unknown"),
        "description": data.get("description", ""),
        "category": data.get("category", ""),
        "preview_url": data.get("preview_url", ""),
        "gender": labels.get("gender", ""),
        "accent": labels.get("accent", ""),
        "age": labels.get("age", ""),
        "use_case": labels.get("use_case", ""),
    }


@app.post("/api/voices/add")
async def add_voice(payload: VoiceAddRequest):
    """Add a voice to the library by looking it up on ElevenLabs."""
    voice_id = payload.voice_id
    custom_name = payload.name.strip()

    api_key = os.getenv("ELEVENLABS_API_KEY")
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.elevenlabs.io/v1/voices/{voice_id}",
            headers={"xi-api-key": api_key},
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Voice not found on ElevenLabs")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ElevenLabs API error: {resp.status_code}")

    data = resp.json()
    labels = data.get("labels", {})
    voice_row = {
        "voice_id": data["voice_id"],
        "name": custom_name or data.get("name", "Unknown"),
        "description": data.get("description", ""),
        "category": data.get("category", ""),
        "preview_url": data.get("preview_url", ""),
        "gender": labels.get("gender", ""),
        "accent": labels.get("accent", ""),
        "age": labels.get("age", ""),
        "use_case": labels.get("use_case", ""),
        "is_active": True,
    }

    result = get_supabase().table("voice_library") \
        .upsert(voice_row, on_conflict="voice_id") \
        .execute()

    return {"status": "added", "voice": result.data[0] if result.data else voice_row}


@app.post("/api/voices/{voice_id}/refresh")
async def refresh_voice(voice_id: str):
    """Re-fetch voice metadata from ElevenLabs and update the local record."""
    existing = get_supabase().table("voice_library") \
        .select("voice_id").eq("voice_id", voice_id).limit(1).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail=f"Voice {voice_id} not found in library")

    api_key = os.getenv("ELEVENLABS_API_KEY")
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.elevenlabs.io/v1/voices/{voice_id}",
            headers={"xi-api-key": api_key},
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Voice no longer exists on ElevenLabs")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ElevenLabs API error: {resp.status_code}")

    data = resp.json()
    labels = data.get("labels", {})
    updates = {
        "name": data.get("name", "Unknown"),
        "description": data.get("description", ""),
        "category": data.get("category", ""),
        "preview_url": data.get("preview_url", ""),
        "gender": labels.get("gender", ""),
        "accent": labels.get("accent", ""),
        "age": labels.get("age", ""),
        "use_case": labels.get("use_case", ""),
    }

    result = get_supabase().table("voice_library") \
        .update(updates).eq("voice_id", voice_id).execute()

    return {"status": "refreshed", "voice_id": voice_id, "voice": result.data[0] if result.data else updates}


@app.delete("/api/voices/{voice_id}")
async def remove_voice(voice_id: str):
    """Soft-delete a voice from the library. Blocked if any agent uses it."""
    agents_using = get_supabase().table("agents") \
        .select("name, display_name") \
        .eq("tts_voice_id", voice_id) \
        .eq("is_active", True) \
        .execute()

    if agents_using.data:
        agent_names = [a["display_name"] for a in agents_using.data]
        raise HTTPException(
            status_code=409,
            detail=f"Cannot remove — voice is in use by: {', '.join(agent_names)}",
        )

    result = get_supabase().table("voice_library") \
        .update({"is_active": False}) \
        .eq("voice_id", voice_id) \
        .execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Voice not found")
    return {"status": "removed", "voice_id": voice_id}


@app.get("/api/voices/{voice_id}/agents")
async def get_voice_agents(voice_id: str):
    """List which agents use a given voice."""
    result = get_supabase().table("agents") \
        .select("id, name, display_name") \
        .eq("tts_voice_id", voice_id) \
        .eq("is_active", True) \
        .execute()
    return {"agents": result.data or []}


# ── Outbound calls ──────────────────────────────────────────────────────────


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


# ── Batch operations ────────────────────────────────────────────────────────


@app.post("/api/batches/upload", response_model=UploadBatchResponse)
async def upload_batch(
    file: UploadFile = File(...),
    agent_name: str = Form(...),
    from_number: str = Form(...),
):
    """Upload Excel/CSV for batch validation and storage."""
    if not os.getenv("SUPABASE_URL") or not os.getenv("SUPABASE_SERVICE_KEY"):
        return JSONResponse(
            status_code=503,
            content={"detail": "Supabase is not configured"},
        )

    if not E164_RE.match(from_number):
        return JSONResponse(status_code=400, content={"detail": "from_number must be E.164 format"})

    fname = (file.filename or "").lower()
    allowed_ext = (".csv", ".xlsx", ".xls", ".xlsm")
    if not any(fname.endswith(ext) for ext in allowed_ext):
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported file type. Please upload CSV or Excel ({', '.join(allowed_ext)})"},
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

    try:
        agent_row = get_supabase().table("agents") \
            .select("display_name").eq("name", agent_name).limit(1).execute()
        agent_display_name = agent_row.data[0].get("display_name", agent_name) if agent_row.data else agent_name
    except Exception:
        agent_display_name = agent_name

    row = {
        "id": batch_id,
        "name": fname,
        "agent_name": agent_name,
        "agent_display_name": agent_display_name,
        "from_number": from_number,
        "status": "draft",
        "total_rows": summary["valid"],
        "completed_rows": 0,
        "failed_rows": 0,
        "rows": payloads,
        "input_file_path": storage_path,
    }
    await insert_batch(row)

    return UploadBatchResponse(
        batch_id=batch_id,
        columns=headers,
        phone_column_index=phone_idx,
        summary=summary,
        rows=[
            UploadRowResponse(
                row_index=p["row_index"],
                validation=p["validation"],
                phone_raw=p.get("phone_raw", ""),
                phone_e164=p["phone_e164"],
                data=p.get("case_data") or {},
            )
            for p in payloads
        ],
    )


@app.put("/api/batches/{batch_id}/rows", response_model=UpdateBatchRowsResponse)
async def update_batch_rows(batch_id: str, payload: UpdateBatchRowsRequest):
    """Single transformation point — frontend rows → runner-ready rows_data."""
    from server.batch_parse import detect_phone_column

    logger.info(
        f"[BATCH ROWS] batch={batch_id} incoming={len(payload.rows)} "
        f"mapping_keys={list(payload.column_mapping.keys())[:5]}"
    )

    variable_map: dict[str, str] = {}
    phone_source_col: str | None = None
    for csv_col, target in payload.column_mapping.items():
        if target == "__phone__":
            phone_source_col = csv_col
        elif target and target != "__skip__":
            variable_map[csv_col] = target

    mapped_rows: list[dict] = []
    skipped_invalid: list[int] = []

    for row in payload.rows:
        if row.excluded:
            continue

        raw_phone = row.phone
        if not raw_phone and phone_source_col:
            raw_phone = row.data.get(phone_source_col, "")
        if not raw_phone:
            keys = list(row.data.keys())
            pi = detect_phone_column(keys)
            if pi is not None and pi < len(keys):
                raw_phone = str(row.data[keys[pi]]).strip()

        phone = normalize_phone(raw_phone)
        if not phone or len(phone) < 12:
            skipped_invalid.append(row.index)
            continue

        if variable_map:
            case_data = {}
            for csv_col, agent_var in variable_map.items():
                val = row.data.get(csv_col, "")
                case_data[agent_var] = str(val).strip() if val else ""
        else:
            case_data = {k: str(v).strip() if v else "" for k, v in row.data.items()}

        mapped_rows.append({
            "row_index": row.index,
            "phone_e164": phone,
            "validation": "valid",
            "case_data": case_data,
        })

    if not mapped_rows:
        detail = "No valid rows to process"
        if skipped_invalid:
            detail += f". {len(skipped_invalid)} rows had invalid phone numbers."
        raise HTTPException(status_code=400, detail=detail)

    if skipped_invalid:
        logger.warning(
            f"[BATCH ROWS] batch={batch_id} skipped {len(skipped_invalid)} "
            f"rows with invalid phones"
        )

    update_result = (
        get_supabase()
        .table("batches")
        .update({
            "rows_data": mapped_rows,
            "column_mapping": payload.column_mapping,
            "total_rows": len(mapped_rows),
            "rows": [],
        })
        .eq("id", batch_id)
        .execute()
    )
    if not update_result.data:
        raise HTTPException(status_code=404, detail=f"Batch not found: {batch_id}")

    logger.info(
        f"[BATCH ROWS] batch={batch_id} saved {len(mapped_rows)} dialable rows"
    )
    return UpdateBatchRowsResponse(
        batch_id=batch_id,
        total_rows=len(mapped_rows),
        skipped_invalid=len(skipped_invalid),
    )


@app.post("/api/batches/{batch_id}/start", response_model=StartBatchResponse)
async def start_batch(batch_id: str, payload: StartBatchRequest):
    """Start (or schedule) a batch with atomic double-click protection."""
    if payload.schedule_mode == "now":
        win_start = "00:00:00"
        win_end = "23:59:59"
        win_days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    else:
        win_start = payload.calling_window_start
        win_end = payload.calling_window_end
        win_days = payload.calling_window_days or [
            "Mon", "Tue", "Wed", "Thu", "Fri"
        ]

    update_fields: dict = {
        "status": "scheduled",
        "concurrency": min(payload.concurrency, MAX_SERVER_CONCURRENCY),
        "timezone": payload.timezone,
        "calling_window_start": win_start,
        "calling_window_end": win_end,
        "calling_window_days": win_days,
        "schedule_mode": payload.schedule_mode,
    }
    if payload.start_date:
        update_fields["start_date"] = payload.start_date

    result = (
        get_supabase()
        .table("batches")
        .update(update_fields)
        .eq("id", batch_id)
        .in_("status", ["draft", "ready"])
        .execute()
    )
    if not result.data:
        check = (
            get_supabase()
            .table("batches")
            .select("status")
            .eq("id", batch_id)
            .limit(1)
            .execute()
        )
        if check.data and check.data[0].get("status") in ("running", "scheduled"):
            return StartBatchResponse(
                status="already_running", batch_id=batch_id
            )
        if not check.data:
            raise HTTPException(status_code=404, detail="batch not found")
        raise HTTPException(
            status_code=400,
            detail=f"batch status is {check.data[0].get('status')!r}, cannot start",
        )

    asyncio.create_task(run_batch(_pending_calls, batch_id))
    return StartBatchResponse(
        status="scheduled",
        batch_id=batch_id,
        total_calls=result.data[0].get("total_rows", 0),
    )


@app.post("/api/batches/{batch_id}/pause", response_model=BatchControlResponse)
async def pause_batch(batch_id: str):
    """User-initiated pause — runner will stop dialing new rows."""
    result = (
        get_supabase()
        .table("batches")
        .update({"paused_by_user": True, "status": "paused"})
        .eq("id", batch_id)
        .in_("status", ["running", "scheduled"])
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=400, detail="Batch cannot be paused")
    return BatchControlResponse(status="paused", batch_id=batch_id)


@app.post("/api/batches/{batch_id}/resume", response_model=BatchControlResponse)
async def resume_batch(batch_id: str):
    """Resume a user-paused batch."""
    result = (
        get_supabase()
        .table("batches")
        .update({"paused_by_user": False, "status": "scheduled"})
        .eq("id", batch_id)
        .eq("status", "paused")
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=400, detail="Batch cannot be resumed")
    return BatchControlResponse(status="resumed", batch_id=batch_id)


@app.post("/api/batches/{batch_id}/cancel", response_model=BatchControlResponse)
async def cancel_batch(batch_id: str):
    """Cancel a running, scheduled, or paused batch."""
    result = (
        get_supabase()
        .table("batches")
        .update({"status": "canceled"})
        .eq("id", batch_id)
        .in_("status", ["running", "scheduled", "paused"])
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=400, detail="Batch cannot be canceled")
    return BatchControlResponse(status="canceled", batch_id=batch_id)


@app.delete("/api/batches/cleanup-drafts")
async def cleanup_draft_batches():
    """Delete abandoned draft/ready batches older than 24 hours."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    result = get_supabase().table("batches") \
        .delete() \
        .in_("status", ["draft", "ready"]) \
        .lt("created_at", cutoff) \
        .execute()
    deleted = len(result.data) if result.data else 0
    return {"status": "cleaned", "deleted": deleted}


@app.get("/api/batches/{batch_id}/status")
async def batch_status(batch_id: str):
    batch = await get_batch(batch_id)
    if not batch:
        return JSONResponse(status_code=404, content={"detail": "batch not found"})
    return {
        "batch_id": batch_id,
        "status": batch.get("status"),
        "agent_name": batch.get("agent_name"),
        "agent_display_name": batch.get("agent_display_name"),
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


# ── Twilio inbound + WebSocket ──────────────────────────────────────────────


_SKIP_TWILIO_VALIDATION = os.getenv("SKIP_TWILIO_VALIDATION", "false").lower() == "true"


@app.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Return TwiML for inbound calls — routes via phone_numbers table."""
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
                f"Invalid Twilio signature on incoming webhook "
                f"from {request.client.host if request.client else 'unknown'}"
            )
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

        form = form_data
    else:
        form = await request.form()
    to_number = str(form.get("To", ""))

    agent_name = None
    if to_number:
        resp = (
            get_supabase()
            .table("phone_numbers")
            .select("inbound_agent_id")
            .eq("number", to_number)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        if resp.data and resp.data[0].get("inbound_agent_id"):
            agent_id = resp.data[0]["inbound_agent_id"]
            agent_resp = (
                get_supabase()
                .table("agents")
                .select("name")
                .eq("id", agent_id)
                .eq("is_active", True)
                .limit(1)
                .execute()
            )
            if agent_resp.data:
                agent_name = agent_resp.data[0]["name"]

    if not agent_name:
        agent_name = os.getenv("DEFAULT_INBOUND_AGENT", "chris/claim_status")

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


# ── WebRTC test calls ───────────────────────────────────────────────────────
#
# The SmallWebRTCTransport client SDK sends ALL requests (connect, SDP offer,
# ICE candidates) to the same endpoint URL.  We detect the request type by
# checking the HTTP method and the presence of an ``sdp`` key in the body.
#
#   POST  (no sdp)  → RTVI connect: validate agent, return sessionId
#   POST  (has sdp) → WebRTC SDP offer/answer exchange
#   PATCH           → ICE trickle candidates

_test_call_sessions: dict[str, dict] = {}


@app.api_route("/api/test-call/connect", methods=["POST", "PATCH"])
async def test_call_connect(request: Request):
    body = await request.json()
    logger.info(f"[TEST CALL] {request.method} body_keys={list(body.keys())} qp={dict(request.query_params)}")

    # ── PATCH → ICE trickle candidates ──────────────────────────────────
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

    # ── POST with SDP → WebRTC offer/answer exchange ────────────────────
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
            import json as _json
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

    # ── POST without SDP → RTVI connect ─────────────────────────────────
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
        import json as _json
        try:
            case_data = _json.loads(case_data)
        except ValueError:
            case_data = {}
    qp_case = qp.get("case_data")
    if qp_case and not case_data:
        import json as _json
        try:
            case_data = _json.loads(qp_case)
        except ValueError:
            case_data = {}

    if not agent_name:
        raise HTTPException(status_code=400, detail="agent_name is required")

    table = "agent_drafts" if use_draft else "agents"
    query = get_supabase().table(table).select("name").eq("name", agent_name).limit(1)
    if not use_draft:
        query = query.eq("is_active", True)
    if not query.execute().data:
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
