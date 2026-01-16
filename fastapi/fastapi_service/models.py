from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str
    user_id: str | None = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    request_id: str
    answer: str
    query_plan: dict[str, Any]
    data: list[dict[str, Any]]


class EventRequest(BaseModel):
    ts: datetime | None = None
    session_id: str
    user_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class StatusResponse(BaseModel):
    status: str
