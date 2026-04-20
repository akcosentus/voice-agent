"""Pydantic schemas for the calling engine endpoints."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ── Outbound call ────────────────────────────────────────────────────────────


class OutboundCallRequest(BaseModel):
    agent_name: str
    target_number: str
    from_number: str
    case_data: dict = Field(default_factory=dict)

    @field_validator("target_number", "from_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not re.match(r"^\+[1-9]\d{1,14}$", v):
            raise ValueError(f"Phone number must be E.164 format (e.g. +19495551234): {v}")
        return v


class OutboundCallResponse(BaseModel):
    call_sid: str
    call_id: str
    status: str
