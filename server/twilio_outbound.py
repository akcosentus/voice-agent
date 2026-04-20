"""Shared Twilio outbound dial + pending_calls registration."""

import os
import uuid

from loguru import logger
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse


def _server_url() -> str:
    return os.getenv("SERVER_URL", "").rstrip("/")


def get_pending_call_by_sid(pending_calls: dict, call_sid: str) -> tuple[str, dict] | None:
    """Find a pending call entry by Twilio Call SID. Returns (our_call_id, entry) or None."""
    for cid, info in pending_calls.items():
        if info.get("twilio_sid") == call_sid:
            return cid, info
    return None


def ws_url_from_env() -> str:
    server_url = os.getenv("SERVER_URL", "")
    return server_url.replace("https://", "wss://").replace("http://", "ws://") + "/ws"


def build_outbound_twiml(ws_url: str, agent_name: str, call_id: str) -> str:
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    stream.parameter(name="agent_name", value=agent_name)
    stream.parameter(name="call_id", value=call_id)
    connect.append(stream)
    response.append(connect)
    response.pause(length=60)
    return str(response)


def place_outbound_call(
    pending_calls: dict,
    *,
    agent_name: str,
    target_number: str,
    case_data: dict,
    from_number: str,
    call_id: str | None = None,
    batch_id: str | None = None,
    batch_row_index: int | None = None,
    batch_row_id: str | None = None,
    request_id: str | None = None,
) -> tuple[str, str]:
    """Store case data in ``pending_calls`` and start a Twilio outbound call.

    Registers a Twilio status callback so we hear about declined, busy,
    no-answer, canceled, and failed calls within seconds instead of
    waiting for a WebSocket that never opens.

    Returns ``(call_id, twilio_call_sid)``.
    """
    cid = call_id or str(uuid.uuid4())
    entry: dict = {
        "agent_name": agent_name,
        "case_data": case_data,
        "target_number": target_number,
    }
    if batch_id is not None:
        entry["batch_id"] = batch_id
    if batch_row_index is not None:
        entry["batch_row_index"] = batch_row_index
    if batch_row_id is not None:
        entry["batch_row_id"] = batch_row_id
    if request_id is not None:
        entry["request_id"] = request_id
    pending_calls[cid] = entry

    twiml = build_outbound_twiml(ws_url_from_env(), agent_name, cid)
    client = TwilioClient(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
    )

    create_kwargs: dict = {
        "to": target_number,
        "from_": from_number,
        "twiml": twiml,
    }

    # If SERVER_URL is configured, register a status callback so we get
    # fast notifications for declined / busy / no-answer / failed calls.
    srv = _server_url()
    if srv:
        create_kwargs.update({
            "status_callback": f"{srv}/api/twilio/status-callback",
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
            "status_callback_method": "POST",
        })

    call = client.calls.create(**create_kwargs)

    # Save the Twilio SID so the status callback can route updates back to
    # our internal call_id + downstream batch_row_id / request_id.
    pending_calls[cid]["twilio_sid"] = call.sid

    logger.info(f"Twilio outbound call_id={cid} sid={call.sid} to={target_number}")
    return cid, call.sid
