from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.api.schemas import (
    NLQPlanRequest,
    NLQPlanResponse,
    QueryRenderRequest,
    QueryRenderResponse,
    StateContext,
    StateContextResponse,
)
from backend.services.cube_service import cube_service
from backend.services.nlq_service import nlq_service
from backend.services.state_service import state_service
from backend.services.viz_service import viz_service

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/nlq/plan", response_model=NLQPlanResponse)
def nlq_plan(request: NLQPlanRequest) -> NLQPlanResponse:
    plan_result, _ = nlq_service.build_plan(request)
    return plan_result


@router.post("/query/render", response_model=QueryRenderResponse)
def query_render(request: QueryRenderRequest) -> QueryRenderResponse:
    plan_result, intent = nlq_service.build_plan(
        NLQPlanRequest(
            session_id=request.session_id,
            message=request.message,
            active_filters=request.active_filters,
            last_selection=request.last_selection,
        )
    )

    state_service.append_message(request.session_id, "user", request.message)
    df, meta = cube_service.execute(plan_result.cube_query)

    chart_type = request.chart_type or intent.chart_type
    figure = viz_service.build_figure(
        df=df,
        cube_query=plan_result.cube_query,
        chart_type=chart_type,
        figure_id="figure-1",
    )

    assistant_message = (
        f"{intent.reasoning} Showing a {chart_type} chart based on the current context."
    )
    state_service.append_message(request.session_id, "assistant", assistant_message)
    context = state_service.update_context(
        session_id=request.session_id,
        active_filters=plan_result.cube_query.filters,
        last_selection=request.last_selection,
    )

    table_payload = {"preview": df.head(20).to_dict(orient="records")}

    return QueryRenderResponse(
        assistant_message=assistant_message,
        active_filters=context.active_filters,
        last_selection=context.last_selection,
        figures=[figure],
        tables=[table_payload],
        plan=plan_result.plan,
    )


@router.get("/state/context", response_model=StateContextResponse)
def get_state(session_id: str) -> StateContextResponse:
    return state_service.get_context(session_id)


@router.post("/state/context", response_model=StateContextResponse)
def update_state(payload: StateContext) -> StateContextResponse:
    return state_service.update_context(
        session_id=payload.session_id,
        active_filters=payload.active_filters,
        last_selection=payload.last_selection,
        conversation=payload.conversation,
    )


@router.post("/state/reset", response_model=StateContextResponse)
def reset_state(payload: StateContext) -> StateContextResponse:
    return state_service.reset(payload.session_id)

