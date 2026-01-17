from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str
    user_id: str | None = None
    message: str
    history: list[dict[str, str]] = Field(default_factory=list)
    active_chart_id: int | None = None
    active_chart_name: str | None = None
    debug: bool = False


class ChatResponse(BaseModel):
    session_id: str
    request_id: str
    answer: str
    query_plan: dict[str, Any]
    data: list[dict[str, Any]]
    debug: list[dict[str, Any]] | None = None


class EventRequest(BaseModel):
    ts: datetime | None = None
    session_id: str
    user_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class StatusResponse(BaseModel):
    status: str
