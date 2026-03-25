"""Shared Twilio outbound dial + pending_calls registration."""

import os
import uuid

from loguru import logger
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse


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
) -> tuple[str, str]:
    """Store case data in ``pending_calls`` and start a Twilio outbound call.

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
    pending_calls[cid] = entry

    twiml = build_outbound_twiml(ws_url_from_env(), agent_name, cid)
    client = TwilioClient(
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
    )
    call = client.calls.create(
        to=target_number,
        from_=from_number,
        twiml=twiml,
    )
    logger.info(f"Twilio outbound call_id={cid} sid={call.sid} to={target_number}")
    return cid, call.sid
