"""Pydantic schemas for the batch pipeline and agent endpoints.

These models define the exact data contract between frontend and backend.
If the frontend sends the wrong shape, FastAPI returns a clear 422 error
with the exact field name, expected type, and what it got.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Batch rows (PUT /api/batches/{id}/rows) ──────────────────────────────────


class BatchRowPayload(BaseModel):
    """A single row as sent by the frontend batch editor."""

    index: int
    phone: str = Field(..., description="Phone number (any format, will be normalized)")
    data: dict[str, str] = Field(default_factory=dict)
    excluded: bool = False

    @field_validator("phone")
    @classmethod
    def strip_phone(cls, v: str) -> str:
        return v.strip()


class UpdateBatchRowsRequest(BaseModel):
    """Request body for PUT /api/batches/{id}/rows."""

    column_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="CSV column → agent variable (or __phone__ / __skip__)",
    )
    rows: list[BatchRowPayload] = Field(
        ..., min_length=1, description="All rows from the editor"
    )


class UpdateBatchRowsResponse(BaseModel):
    status: str = "saved"
    batch_id: str
    total_rows: int
    skipped_invalid: int = 0


# ── Batch start (POST /api/batches/{id}/start) ──────────────────────────────


class StartBatchRequest(BaseModel):
    """Request body for POST /api/batches/{id}/start."""

    concurrency: int = Field(default=5, ge=1, le=10)
    schedule_mode: str = Field(default="now", pattern=r"^(now|scheduled)$")
    timezone: str = "America/New_York"
    calling_window_start: str = "09:00:00"
    calling_window_end: str = "17:00:00"
    calling_window_days: Optional[list[str]] = None
    start_date: Optional[str] = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except (KeyError, Exception):
            raise ValueError(
                f"Invalid timezone: '{v}'. Use IANA format like 'America/New_York'"
            )
        return v

    @field_validator("calling_window_days", mode="before")
    @classmethod
    def default_and_validate_days(cls, v: Any, info: Any) -> list[str]:
        if v is not None:
            if isinstance(v, list) and len(v) == 0:
                raise ValueError("At least one calling day is required")
            return v
        mode = info.data.get("schedule_mode", "now") if hasattr(info, "data") else "now"
        if mode == "now":
            return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        return ["Mon", "Tue", "Wed", "Thu", "Fri"]


class StartBatchResponse(BaseModel):
    status: str
    batch_id: str
    total_calls: int = 0


# ── Batch upload (POST /api/batches/upload) ──────────────────────────────────


class UploadRowResponse(BaseModel):
    row_index: int
    validation: str
    phone_raw: str = ""
    phone_e164: str = ""
    data: dict[str, str] = Field(default_factory=dict)


class UploadBatchResponse(BaseModel):
    batch_id: str
    columns: list[str]
    phone_column_index: Optional[int] = None
    summary: dict[str, int]
    rows: list[UploadRowResponse]


# ── Batch control (pause / resume / cancel) ──────────────────────────────────


class BatchControlResponse(BaseModel):
    status: str
    batch_id: str


# ── Runner internal: the shape of each row in rows_data ──────────────────────


class DialableRow(BaseModel):
    """The final shape of a row in rows_data — what the batch runner reads."""

    row_index: int
    phone_e164: str
    case_data: dict[str, str]
    validation: str = "valid"


# ── Agent create (POST /api/agents) ──────────────────────────────────────────


class CreateAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    display_name: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9\-/_]*$", v):
            raise ValueError(
                "Agent name must be lowercase alphanumeric with hyphens, "
                "underscores, or slashes"
            )
        return v


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


# ── Agent update (PUT /api/agents/{name}) ────────────────────────────────────


class UpdateAgentRequest(BaseModel):
    """Partial update — only provided fields are written. All optional."""

    display_name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    first_message: Optional[str] = None
    llm_model: Optional[str] = None
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(None, ge=50, le=1000)
    tts_voice_id: Optional[str] = None
    tts_model: Optional[str] = None
    tts_stability: Optional[float] = Field(None, ge=0.0, le=1.0)
    tts_similarity_boost: Optional[float] = Field(None, ge=0.0, le=1.0)
    tts_style: Optional[float] = Field(None, ge=0.0, le=1.0)
    tts_speed: Optional[float] = Field(None, ge=0.7, le=1.2)
    tts_use_speaker_boost: Optional[bool] = None
    stt_language: Optional[str] = None
    stt_keywords: Optional[list[str]] = None
    tools: Optional[list[dict[str, Any]]] = None
    post_call_analyses: Optional[dict[str, Any]] = None
    idle_timeout_secs: Optional[int] = Field(None, ge=10, le=120)
    idle_message: Optional[str] = None
    max_call_duration_secs: Optional[int] = Field(None, ge=60, le=3600)
    voicemail_action: Optional[str] = None
    voicemail_message: Optional[str] = None
    max_retries: Optional[int] = Field(None, ge=0, le=10)
    retry_delay_secs: Optional[int] = Field(None, ge=60, le=3600)
    default_concurrency: Optional[int] = Field(None, ge=1, le=10)
    calling_window_start: Optional[str] = None
    calling_window_end: Optional[str] = None
    calling_window_days: Optional[list[str]] = None


# ── Agent prompt (PUT /api/agents/{name}/prompt) ─────────────────────────────


class UpdatePromptRequest(BaseModel):
    content: str


# ── Agent clone (POST /api/agents/{name}/clone) ─────────────────────────────


class CloneAgentRequest(BaseModel):
    name: Optional[str] = None
    display_name: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^[a-z0-9][a-z0-9\-/_]*$", v):
            raise ValueError(
                "Agent name must be lowercase alphanumeric with hyphens, "
                "underscores, or slashes"
            )
        return v


# ── Phone numbers ────────────────────────────────────────────────────────────


class CreatePhoneNumberRequest(BaseModel):
    number: str = Field(..., min_length=10)
    friendly_name: str = ""
    inbound_agent_id: Optional[str] = None
    outbound_agent_id: Optional[str] = None

    @field_validator("number")
    @classmethod
    def validate_e164(cls, v: str) -> str:
        if not re.match(r"^\+[1-9]\d{1,14}$", v):
            raise ValueError("number must be E.164 format (e.g. +19495551234)")
        return v


class UpdatePhoneNumberRequest(BaseModel):
    friendly_name: Optional[str] = None
    inbound_agent_id: Optional[str] = None
    outbound_agent_id: Optional[str] = None


class PurchasePhoneNumberRequest(BaseModel):
    number: str = Field(..., min_length=10)
    friendly_name: str = ""

    @field_validator("number")
    @classmethod
    def validate_e164(cls, v: str) -> str:
        if not re.match(r"^\+[1-9]\d{1,14}$", v):
            raise ValueError("number must be E.164 format (e.g. +19495551234)")
        return v


# ── Voice library ────────────────────────────────────────────────────────────


class VoiceLookupRequest(BaseModel):
    voice_id: str = Field(..., min_length=1)

    @field_validator("voice_id")
    @classmethod
    def strip_id(cls, v: str) -> str:
        return v.strip()


class VoiceAddRequest(BaseModel):
    voice_id: str = Field(..., min_length=1)
    name: str = ""

    @field_validator("voice_id")
    @classmethod
    def strip_id(cls, v: str) -> str:
        return v.strip()


# ── Call list / detail response models ───────────────────────────────────────


class CallSummary(BaseModel):
    id: str
    agent_name: str
    agent_display_name: Optional[str] = None
    target_number: Optional[str] = None
    direction: Optional[str] = None
    status: str
    duration_secs: Optional[float] = None
    batch_id: Optional[str] = None
    batch_row_index: Optional[int] = None
    error: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


class CallDetail(CallSummary):
    transcript: Optional[list[dict[str, Any]]] = None
    post_call_analyses: Optional[dict[str, Any]] = None
    recording_path: Optional[str] = None
    case_data: Optional[dict[str, Any]] = None


class PaginatedCallsResponse(BaseModel):
    calls: list[CallSummary]
    total: int
    page: int
    page_size: int


class CallAgentEntry(BaseModel):
    display_name: str
    agent_name: str


class CallAgentNamesResponse(BaseModel):
    agents: list[CallAgentEntry]


# ── Batch list / detail response models ──────────────────────────────────────


class BatchSummary(BaseModel):
    id: str
    name: Optional[str] = None
    agent_name: str
    agent_display_name: Optional[str] = None
    status: str
    total_rows: int = 0
    completed_rows: int = 0
    failed_rows: int = 0
    concurrency: int = 1
    schedule_mode: Optional[str] = None
    from_number: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BatchDetailResponse(BaseModel):
    batch: BatchSummary
    calls: list[CallSummary]


class BatchListResponse(BaseModel):
    batches: list[BatchSummary]


class SignedUrlResponse(BaseModel):
    url: str


class DraftDeleteResponse(BaseModel):
    status: str
    batch_id: Optional[str] = None
