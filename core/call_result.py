"""Universal contract for a completed (or failed) voice call."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class CallResult:
    call_id: str
    agent_name: str
    target_number: str
    agent_display_name: str = ""
    direction: str = "outbound"  # outbound | inbound
    status: str = "pending"  # pending, in_progress, completed, failed, no_answer
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_secs: Optional[float] = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    recording_path: Optional[str] = None
    case_data: dict[str, Any] = field(default_factory=dict)
    post_call_analyses: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    batch_id: Optional[str] = None
    batch_row_index: Optional[int] = None
