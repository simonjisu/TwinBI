from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from pydantic.config import ConfigDict


class ChatRequest(BaseModel):
    session_id: str
    user_id: str | None = None
    message: str
    history: list[dict[str, str]] = Field(default_factory=list)
    dashboard_id: int | None = None
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


class SupersetDatasetQuery(BaseModel):
    columns: list[Any] = Field(default_factory=list)
    metrics: list[Any] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    orderby: list[Any] = Field(default_factory=list)
    row_limit: int | None = None
    extras: dict[str, Any] | None = None
    result_format: str | None = None
    result_type: str | None = None
    queries: list[dict[str, Any]] | None = None


class ViewCubeInclude(BaseModel):
    join_path: str
    includes: list[str] = Field(default_factory=list)
    prefix: bool = False


class ViewSpec(BaseModel):
    view_name: str
    base_cube: str
    description: str | None = None
    measures: list[str] = Field(default_factory=list)
    dimensions: list[ViewCubeInclude] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    governance: dict[str, Any] | None = None
    superset_sync: dict[str, Any] | None = None


class CreateViewResult(BaseModel):
    status: str
    view_name: str
    view_file: str
    cube_reload_status: str
    physical_name: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SupersetDatasetSyncRequest(BaseModel):
    database_id: int
    schema_name: str | None = Field(default=None, alias="schema")
    table_name: str
    dataset_name: str | None = None
    force_refresh: bool = False

    model_config = ConfigDict(populate_by_name=True)


class SupersetDatasetSyncResult(BaseModel):
    status: str
    dataset_id: int | None = None
    created: bool = False
    updated: bool = False
    warnings: list[str] = Field(default_factory=list)


class CreateViewAndSyncRequest(BaseModel):
    view: ViewSpec
    superset: SupersetDatasetSyncRequest | None = None


class CreateViewAndSyncResult(BaseModel):
    status: str
    view_result: CreateViewResult | None = None
    superset_result: SupersetDatasetSyncResult | None = None
