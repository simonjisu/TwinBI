from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class CubeFilter(BaseModel):
    member: str
    operator: str = "equals"
    values: List[Any] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class TimeDimension(BaseModel):
    dimension: str
    dateRange: Optional[List[str]] = None
    granularity: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class CubeQuery(BaseModel):
    measures: List[str] = Field(default_factory=list)
    dimensions: List[str] = Field(default_factory=list)
    timeDimensions: List[TimeDimension] = Field(default_factory=list)
    filters: List[CubeFilter] = Field(default_factory=list)
    order: Dict[str, str] = Field(default_factory=dict)
    limit: Optional[int] = None

    model_config = ConfigDict(extra="ignore")


class SelectionPayload(BaseModel):
    figure_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    derived_filters: List[CubeFilter] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class NLQPlanRequest(BaseModel):
    session_id: str
    message: str
    active_filters: List[CubeFilter] = Field(default_factory=list)
    last_selection: Optional[SelectionPayload] = None


class NLQPlanResponse(BaseModel):
    plan: List[str]
    cube_query: CubeQuery
    suggested_chart: str
    reasoning: str


class QueryRenderRequest(BaseModel):
    session_id: str
    message: str
    active_filters: List[CubeFilter] = Field(default_factory=list)
    last_selection: Optional[SelectionPayload] = None
    chart_type: Optional[str] = None


class FigureResponse(BaseModel):
    figure_id: str
    plotly_spec: Dict[str, Any]
    interaction_mapping: Optional[Dict[str, Any]] = None
    query: CubeQuery
    meta: Dict[str, Any] = Field(default_factory=dict)


class QueryRenderResponse(BaseModel):
    assistant_message: str
    active_filters: List[CubeFilter] = Field(default_factory=list)
    last_selection: Optional[SelectionPayload] = None
    figures: List[FigureResponse] = Field(default_factory=list)
    tables: List[Dict[str, Any]] = Field(default_factory=list)
    plan: List[str] = Field(default_factory=list)


class StateContext(BaseModel):
    session_id: str
    active_filters: List[CubeFilter] = Field(default_factory=list)
    last_selection: Optional[SelectionPayload] = None
    conversation: List[Dict[str, Any]] = Field(default_factory=list)


class StateContextResponse(BaseModel):
    session_id: str
    active_filters: List[CubeFilter] = Field(default_factory=list)
    last_selection: Optional[SelectionPayload] = None
    conversation: List[Dict[str, Any]] = Field(default_factory=list)
