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
    superset_username: str | None = None
    superset_password: str | None = None
    agent_model: str | None = None
    debug: bool = False


class ChatResponse(BaseModel):
    session_id: str
    request_id: str
    answer: str
    query_plan: dict[str, Any]
    data: list[dict[str, Any]]
    debug: list[dict[str, Any]] | None = None


class DashboardOnlyRequest(BaseModel):
    message: str
    visible_evidence: str
    model: str | None = None


class DashboardOnlyResponse(BaseModel):
    answer: str
    model: str
    reasoning_effort: str


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
    superset_username: str | None = None
    superset_password: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class SupersetDatasetSyncResult(BaseModel):
    status: str
    dataset_id: int | None = None
    created: bool = False
    updated: bool = False
    warnings: list[str] = Field(default_factory=list)


class ChartCreateRequest(BaseModel):
    dataset_id: int
    slice_name: str
    viz_type: str
    datasource_type: str = "table"
    encodings: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    owners: list[int] | None = None
    dashboard_id: int | None = None


class ChartCreateResponse(BaseModel):
    status: str
    chart_id: int | None = None
    slice_name: str | None = None
    warnings: list[str] = Field(default_factory=list)


class DashboardAppendChartRequest(BaseModel):
    chart_id: int
    tab_id: str | None = None
    tab_name: str | None = None
    width: int = 4
    height: int = 50


class DashboardAppendChartResponse(BaseModel):
    status: str
    dashboard_id: int
    chart_id: int
    container_id: str | None = None
    row_id: str | None = None
    chart_node_id: str | None = None
    tab: dict[str, Any] | None = None
