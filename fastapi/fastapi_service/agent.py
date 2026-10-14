from __future__ import annotations
import duckdb
import asyncio
import json
import os
import re
import sys
from contextvars import ContextVar
from pathlib import Path
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from fastapi_service.superset import (
    append_chart_to_dashboard_layout,
    fetch_chart_data_from_log,
    fetch_dashboard_charts,
    fetch_dashboard_layout,
    fetch_dashboard_default_tab,
    fetch_dataset_data,
    fetch_dataset_schema,
    fetch_chart_form_data,
    fetch_chart_queries,
)
from fastapi_service.semantic import fetch_cube_meta, run_cube_query
from fastapi_service.cube_conf import load_repo_schema
from fastapi_service.models import (
    ViewSpec,
    SupersetDatasetSyncRequest,
    ChartCreateRequest,
    DashboardAppendChartRequest,
)
from fastapi_service.semantic import (
    create_cube_view as semantic_create_view,
    delete_cube_view as semantic_delete_view,
    sync_superset_dataset as semantic_sync_dataset,
    log_create_view,
    log_dataset_sync,
    log_unified_event,
)
from fastapi_service.superset import lookup_user_id
from fastapi_service.context_manager import (
    attach_aer_records,
    build_answer_evidence_payload,
    refresh_aer_store,
)
from fastapi_service.superset_client import (
    _api_session_with_bearer,
    _ensure_csrf,
    _get_base_url,
)
from fastapi_service import prompts

_DOCS_ROOT = Path(__file__).resolve().parents[1] / "docs"
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
if _SRC_ROOT.exists():
    sys.path.insert(0, str(_SRC_ROOT))

try:
    from src.schema_processor import SchemaExplorer
except Exception as exc:  # pragma: no cover - optional dependency
    SchemaExplorer = None
    _SCHEMA_IMPORT_ERROR = exc
else:
    _SCHEMA_IMPORT_ERROR = None

try:
    from agents import Agent, ModelSettings, Runner, function_tool, RunItemStreamEvent
except Exception as exc:  # pragma: no cover - optional dependency
    Agent = None
    ModelSettings = None
    Runner = None
    function_tool = None
    RunItemStreamEvent = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

class Answer(BaseModel):
    answer: str = Field(..., description="Final answer.")


@dataclass
class AgentContext:
    settings: Any
    conn: Any
    trace_logs: list[dict[str, Any]] | None = None
    active_chart: dict[str, Any] | None = None
    aer_candidates: list[dict[str, Any]] | None = None
    messages: list[dict[str, str]] | None = None
    mask_active_state_for_evaluation: bool = False


_ACTIVE_CONTEXT_VAR: ContextVar[AgentContext | None] = ContextVar(
    "active_agent_context", default=None
)


def _get_active_context() -> AgentContext | None:
    return _ACTIVE_CONTEXT_VAR.get()


def _active_state_masked(context: AgentContext | None) -> bool:
    return bool(context and context.mask_active_state_for_evaluation)


def _get_active_settings() -> tuple[Any | None, dict[str, Any] | None]:
    context = _get_active_context()
    if context is None:
        return None, {"error": "active context not set"}
    if not context.settings:
        return None, {"error": "settings not available"}
    return context.settings, None


def _read_doc_file(path: Path) -> str | dict[str, Any]:
    if not path.is_absolute():
        path = _DOCS_ROOT / path.name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"error": f"doc not found: {path}"}
    except Exception as exc:
        return {"error": f"failed to read doc: {exc}"}


def _infer_chart_seed_from_columns(
    dataset_id: int,
    view_name: str,
    columns: list[str],
) -> dict[str, Any]:
    lowered = [str(col).strip() for col in columns if isinstance(col, str) and col.strip()]
    time_candidates = [
        col
        for col in lowered
        if any(token in col.lower() for token in ("date", "time", "month", "quarter", "year"))
    ]
    groupby_candidates = [
        col
        for col in lowered
        if any(
            token in col.lower()
            for token in ("category", "department", "brand", "type", "state", "region")
        )
    ]
    metric_candidates = [
        col
        for col in lowered
        if any(
            token in col.lower()
            for token in (
                "sales",
                "revenue",
                "receipt",
                "amount",
                "units",
                "count",
                "qty",
                "growth",
                "rate",
            )
        )
    ]

    time_column = time_candidates[0] if time_candidates else None
    metric_column = metric_candidates[0] if metric_candidates else None
    groupby = groupby_candidates[:1]
    sample_encodings: dict[str, Any] = {}
    if time_column:
        sample_encodings["time_column"] = time_column
    if groupby:
        sample_encodings["groupby"] = groupby
    if metric_column:
        sample_encodings["metrics"] = [
            {
                "expressionType": "SIMPLE",
                "aggregate": "SUM",
                "column": {"column_name": metric_column},
                "label": f"SUM({metric_column})",
            }
        ]

    return {
        "dataset_id": dataset_id,
        "datasource_type": "table",
        "hints": {
            "time_columns": time_candidates[:5],
            "groupby_columns": groupby_candidates[:5],
            "metric_columns": metric_candidates[:5],
        },
        "sample_chart_request": {
            "dataset_id": dataset_id,
            "slice_name": f"{view_name} trend",
            "viz_type": "line",
            "encodings": sample_encodings,
            "options": {"time_grain_sqla": "P1M", "time_range": "No filter"},
        },
    }


def _load_chart_templates() -> dict[str, Any]:
    candidates = [
        Path(__file__).resolve().parents[1] / "chart_templates.json",
        Path.cwd() / "fastapi" / "chart_templates.json",
        Path.cwd() / "chart_templates.json",
    ]
    for path in candidates:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def _fetch_latest_chart_log_payload(
    conn: duckdb.DuckDBPyConnection,
    chart_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT json
        FROM superset_action_logs
        WHERE slice_id = ? AND action = 'ChartDataRestApi.data'
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        [chart_id],
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def _fetch_latest_dashboard_id(conn: duckdb.DuckDBPyConnection) -> int | None:
    row = conn.execute(
        """
        SELECT dashboard_id
        FROM superset_action_logs
        WHERE dashboard_id IS NOT NULL
        ORDER BY superset_log_id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return row[0]


def _fetch_latest_active_chart(
    conn: duckdb.DuckDBPyConnection,
    dashboard_id: int | None = None,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT superset_log_id, dttm, action, dashboard_id, slice_id, json
        FROM superset_action_logs
        WHERE slice_id IS NOT NULL
        ORDER BY superset_log_id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    payload = None
    raw_json = row[5]
    if isinstance(raw_json, str) and raw_json:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            payload = None
    result = {
        "superset_log_id": row[0],
        "dttm": row[1].isoformat() if row[1] else None,
        "action": row[2],
        "dashboard_id": row[3],
        "slice_id": row[4],
        "payload": payload,
    }
    if dashboard_id is None or str(row[3]) == str(dashboard_id):
        return result
    return None


def _fetch_latest_ui_event(
    conn: duckdb.DuckDBPyConnection,
    dashboard_id: int | None = None,
    session_id: str | None = None,
    actions: tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    default_ui_actions = (
        "superset_tab_click",
        "legend_toggle",
        "legend_toggle_activate",
        "legend_toggle_deactivate",
        "chart_click",
        "cross_filter_added",
        "cross_filter_removed",
        "drill_to_detail",
        "drill_by",
        "native_filter_added",
        "native_filter_removed",
        "global_filter_added",
        "global_filter_removed",
        # backward compatibility with old names
        "filter_added",
        "filter_removed",
    )
    ui_actions = actions or default_ui_actions
    if not ui_actions:
        return None
    params: list[Any] = list(ui_actions)
    filters = [f"action IN ({','.join(['?'] * len(ui_actions))})"]
    if dashboard_id is not None:
        filters.append("dashboard_id = ?")
        params.append(dashboard_id)
    if isinstance(session_id, str) and session_id.strip():
        filters.append("json_extract_string(json, '$.session_id') = ?")
        params.append(session_id.strip())
    where_clause = " AND ".join(filters)
    row = conn.execute(
        f"""
        SELECT superset_log_id, dttm, action, user_id, dashboard_id, slice_id, json
        FROM superset_action_logs
        WHERE {where_clause}
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    payload = None
    raw_json = row[6]
    if isinstance(raw_json, str) and raw_json:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            payload = None
    return {
        "superset_log_id": row[0],
        "dttm": row[1].isoformat() if row[1] else None,
        "action": row[2],
        "user_id": row[3],
        "dashboard_id": row[4],
        "slice_id": row[5],
        "payload": payload.get("payload") if isinstance(payload, dict) else None,
    }


def _ui_payload(event: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("payload")
    if isinstance(inner, dict):
        return inner
    return payload


def _ui_slice_id(event: dict[str, Any] | None) -> Any:
    if not isinstance(event, dict):
        return None
    if event.get("slice_id") is not None:
        return event.get("slice_id")
    payload = _ui_payload(event)
    for key in ("slice_id", "sliceId", "chart_id", "chartId", "source_slice_id"):
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _normalize_filter_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    col = entry.get("col") or entry.get("subject")
    if col is None:
        return None
    op = entry.get("op") or entry.get("operator") or "IN"
    val = entry.get("val")
    if val is None and "comparator" in entry:
        val = entry.get("comparator")
    if isinstance(val, str):
        text = val.strip()
        if not text or text.lower() == "no filter":
            return None
        val = text
    elif isinstance(val, list):
        cleaned: list[Any] = []
        for item in val:
            if isinstance(item, str):
                text = item.strip()
                if not text or text.lower() == "no filter":
                    continue
                cleaned.append(text)
            elif item not in (None, ""):
                cleaned.append(item)
        if not cleaned:
            return None
        val = cleaned
    elif val in (None, "", []):
        return None
    return {"col": str(col), "op": str(op), "val": val}


def _looks_like_display_text_col(col: str) -> bool:
    text = (col or "").strip()
    if not text:
        return True
    if "," in text and "_" not in text and "." not in text:
        return True
    return False


def _filter_key(entry: dict[str, Any]) -> str:
    return json.dumps(
        {
            "col": entry.get("col"),
            "op": entry.get("op"),
            "val": entry.get("val"),
        },
        sort_keys=True,
        default=str,
    )


def _extract_filters_from_chart_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    form_data = payload.get("form_data") or payload.get("from_data") or {}
    if not isinstance(form_data, dict):
        return []
    filters = form_data.get("filters")
    if not isinstance(filters, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in filters:
        normalized = _normalize_filter_entry(raw)
        if normalized:
            out.append(normalized)
    dedup: dict[str, dict[str, Any]] = {}
    for item in out:
        dedup[_filter_key(item)] = item
    return list(dedup.values())


def _fetch_latest_chart_filters_by_slice(
    conn: duckdb.DuckDBPyConnection,
    slice_ids: list[int],
    dashboard_id: int | None = None,
) -> dict[int, list[dict[str, Any]]]:
    if not slice_ids:
        return {}
    unique_slice_ids: list[int] = []
    seen_slice_ids: set[int] = set()
    for value in slice_ids:
        try:
            sid = int(value)
        except Exception:
            continue
        if sid in seen_slice_ids:
            continue
        seen_slice_ids.add(sid)
        unique_slice_ids.append(sid)
    if not unique_slice_ids:
        return {}
    placeholders = ",".join(["?"] * len(unique_slice_ids))
    params: list[Any] = []
    where = [
        "action = 'ChartDataRestApi.data'",
        f"slice_id IN ({placeholders})",
    ]
    params.extend(unique_slice_ids)
    if dashboard_id is not None:
        where.append("dashboard_id = ?")
        params.append(dashboard_id)
    rows = conn.execute(
        f"""
        SELECT superset_log_id, slice_id, json
        FROM superset_action_logs
        WHERE {' AND '.join(where)}
        ORDER BY superset_log_id DESC
        LIMIT 5000
        """,
        params,
    ).fetchall()
    out: dict[int, list[dict[str, Any]]] = {}
    seen: set[int] = set()
    for _, slice_id, raw_json in rows:
        if slice_id is None:
            continue
        sid = int(slice_id)
        if sid in seen:
            continue
        seen.add(sid)
        payload: dict[str, Any] = {}
        if isinstance(raw_json, str) and raw_json:
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                payload = {}
        out[sid] = _extract_filters_from_chart_payload(payload)
        if len(seen) == len(unique_slice_ids):
            break
    return out


def _fetch_current_global_filters(
    conn: duckdb.DuckDBPyConnection,
    dashboard_id: int | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    filter_actions = (
        "native_filter_added",
        "native_filter_removed",
        "global_filter_added",
        "global_filter_removed",
    )
    params: list[Any] = list(filter_actions)
    where = [f"action IN ({','.join(['?'] * len(filter_actions))})"]
    if dashboard_id is not None:
        where.append("dashboard_id = ?")
        params.append(dashboard_id)
    if isinstance(session_id, str) and session_id.strip():
        where.append("json_extract_string(json, '$.session_id') = ?")
        params.append(session_id.strip())
    rows = conn.execute(
        f"""
        SELECT action, json
        FROM superset_action_logs
        WHERE {' AND '.join(where)}
        ORDER BY superset_log_id ASC
        """,
        params,
    ).fetchall()
    active: dict[str, dict[str, Any]] = {}
    for action, raw_json in rows:
        payload_obj: dict[str, Any] = {}
        if isinstance(raw_json, str) and raw_json:
            try:
                payload_obj = json.loads(raw_json)
            except json.JSONDecodeError:
                payload_obj = {}
        payload = payload_obj.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
            payload = payload.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        normalized = _normalize_filter_entry(payload.get("filter"))
        if not normalized:
            continue
        if _looks_like_display_text_col(str(normalized.get("col") or "")):
            continue
        key = _filter_key(normalized)
        if action in ("native_filter_added", "global_filter_added"):
            active[key] = normalized
        elif action in ("native_filter_removed", "global_filter_removed"):
            active.pop(key, None)
    return list(active.values())


def _normalize_legend_interaction(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    name = payload.get("legend_name")
    active = payload.get("legend_active")
    if name is None and active is None:
        return None
    normalized = dict(payload)
    if isinstance(normalized.get("legend_name"), str):
        normalized["legend_name"] = normalized["legend_name"].strip()
    return normalized


def _active_context_payload(context: AgentContext | None) -> dict[str, Any]:
    if context and isinstance(context.active_chart, dict):
        return context.active_chart
    return {}


def _pick_context_chart(context: AgentContext | None) -> dict[str, Any] | None:
    active_context = _active_context_payload(context)
    charts = active_context.get("active_charts")
    if not isinstance(charts, list) or not charts:
        return None
    for chart in charts:
        if isinstance(chart, dict) and chart.get("slice_id") and chart.get("interaction"):
            return chart
    for chart in charts:
        if isinstance(chart, dict) and chart.get("slice_id") is not None:
            return chart
    return None


def _pick_context_chart_id(context: AgentContext | None) -> int | None:
    chart = _pick_context_chart(context)
    if not isinstance(chart, dict):
        return None
    try:
        return int(chart.get("slice_id"))
    except Exception:
        return None


if function_tool:
    _SCHEMA_EXPLORER_ERROR: Exception | None = None
    _SCHEMA_EXPLORER: SchemaExplorer | None = None
    if SchemaExplorer is None:
        _SCHEMA_EXPLORER_ERROR = _SCHEMA_IMPORT_ERROR
    else:
        try:
            _SCHEMA_EXPLORER = SchemaExplorer(Path("./data/sales"), schema_type="star")
        except Exception as exc:  # pragma: no cover - optional data dependency
            _SCHEMA_EXPLORER_ERROR = exc

    def _schema_explorer_or_error() -> tuple[SchemaExplorer | None, dict[str, Any] | None]:
        if _SCHEMA_EXPLORER is not None:
            return _SCHEMA_EXPLORER, None
        if _SCHEMA_EXPLORER_ERROR:
            return None, {"error": f"schema explorer not available: {_SCHEMA_EXPLORER_ERROR}"}
        return None, {"error": "schema explorer not available"}

    @function_tool
    def get_active_chart_log() -> dict[str, Any]:
        """
        Return the latest Superset log payload for the focused chart in active tab context.

        Input:
        - Uses the active context set by the API handler.
        - Focused chart selection priority: interaction chart -> first chart in active tab.

        Output:
        - {"chart_id": int, "chart_name": str | null, "payload": dict | null}
        - {"error": "..."} on missing active chart context.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if _active_state_masked(context):
            return {"error": "active dashboard state is masked for evaluation"}
        chart = _pick_context_chart(context)
        chart_id = _pick_context_chart_id(context)
        if chart_id is None:
            return {"error": "no active tab chart found; call get_active_tab_charts first"}
        payload = _fetch_latest_chart_log_payload(context.conn, chart_id)
        return {
            "chart_id": chart_id,
            "chart_name": chart.get("name") if isinstance(chart, dict) else None,
            "payload": payload,
        }

    @function_tool
    def get_active_chart_data() -> dict[str, Any]:
        """
        Fetch chart data for the focused chart in active tab context.

        Input:
        - Uses the active context set by the API handler.
        - Focused chart selection priority: interaction chart -> first chart in active tab.

        Output:
        - {"chart_id": int, "chart_name": str | null, "data": dict}
        - {"error": "..."} on missing active chart context.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if _active_state_masked(context):
            return {"error": "active dashboard state is masked for evaluation"}
        chart = _pick_context_chart(context)
        chart_id = _pick_context_chart_id(context)
        if chart_id is None:
            return {"error": "no active tab chart found; call get_active_tab_charts first"}
        payload = _fetch_latest_chart_log_payload(context.conn, chart_id)
        if not payload:
            return {"error": "no chart log payload found"}
        data = fetch_chart_data_from_log(context.settings, payload)
        return {
            "chart_id": chart_id,
            "chart_name": chart.get("name") if isinstance(chart, dict) else None,
            "data": data["raw"],
        }

    @function_tool
    def get_chart_sql() -> dict[str, Any]:
        """
        Return the latest SQL/query for the focused chart in active tab context.

        Input:
        - Uses the active context set by the API handler.
        - Focused chart selection priority: interaction chart -> first chart in active tab.

        Output:
        - {"chart_id": int, "chart_name": str | null, "sql": str | null}
        - {"error": "..."} if no active chart context or no log payload.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if _active_state_masked(context):
            return {"error": "active dashboard state is masked for evaluation"}
        chart = _pick_context_chart(context)
        chart_id = _pick_context_chart_id(context)
        if chart_id is None:
            return {"error": "no active tab chart found; call get_active_tab_charts first"}
        row = context.conn.execute(
            """
            SELECT json
            FROM superset_action_logs
            WHERE slice_id = ? AND action = 'ChartDataRestApi.data'
            ORDER BY superset_log_id DESC
            LIMIT 1
            """,
            [chart_id],
        ).fetchone()
        if not row or not row[0]:
            return {"error": "no chart log payload found"}
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return {"error": "log payload is not valid JSON"}
        return {
            "chart_id": chart_id,
            "chart_name": chart.get("name") if isinstance(chart, dict) else None,
            "sql": payload.get("sql") or payload.get("query"),
        }

    @function_tool
    def list_dashboard_charts() -> dict[str, Any]:
        """
        List charts for the most relevant dashboard.

        Input:
        - Uses the active context to resolve the dashboard id:
          1) settings.superset_log_dashboard_id if configured
          2) latest dashboard id from DuckDB logs

        Output:
        - {"dashboard_id": int, "charts": [..]}
        - {"error": "..."} if no dashboard id is available.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        dashboard_id = context.settings.superset_log_dashboard_id
        if not dashboard_id:
            dashboard_id = _fetch_latest_dashboard_id(context.conn)
        if not dashboard_id:
            return {"error": "dashboard id not available"}
        charts = fetch_dashboard_charts(context.settings, int(dashboard_id))
        return {"dashboard_id": int(dashboard_id), "charts": charts}

    @function_tool
    def get_dashboard_layout(dashboard_id: int) -> dict[str, Any]:
        """
        Return compact dashboard layout (parsed position_json).

        Input:
        - dashboard_id: Superset dashboard id.

        Output:
        - {"dashboard_id": int, "layout": {...}}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        if not settings.superset_username or not settings.superset_password:
            return {"error": "Superset credentials not configured"}
        if not (settings.superset_internal_url or settings.superset_public_url):
            return {"error": "Superset URL not configured"}
        try:
            return fetch_dashboard_layout(settings, int(dashboard_id))
        except Exception as exc:
            return {"error": f"Superset API error: {exc}"}

    @function_tool
    def get_active_tab_charts() -> dict[str, Any]:
        """
        Return charts for the active tab (from latest tab click or default tab).

        Output:
        - {"dashboard_id": int, "active_tab": {...}, "active_charts": [...]}
          active_charts include native_filters, cross_filters, and interaction.
        - {"error": "..."} on missing dashboard id.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if _active_state_masked(context):
            return {"error": "active dashboard state is masked for evaluation"}
        active_context = context.active_chart if isinstance(context.active_chart, dict) else {}
        if isinstance(active_context, dict) and active_context.get("active_charts") is not None:
            return active_context
        session_id = (
            active_context.get("session_id")
            if isinstance(active_context.get("session_id"), str)
            else None
        )
        dashboard_id = context.settings.superset_log_dashboard_id
        if not dashboard_id and active_context.get("dashboard_id") is not None:
            try:
                dashboard_id = int(active_context.get("dashboard_id"))
            except (TypeError, ValueError):
                dashboard_id = None
        if not dashboard_id:
            dashboard_id = _fetch_latest_dashboard_id(context.conn)
        if not dashboard_id:
            return {"error": "dashboard id not available"}

        charts = fetch_dashboard_charts(context.settings, int(dashboard_id))
        tab_event = _fetch_latest_ui_event(
            context.conn,
            dashboard_id=int(dashboard_id),
            session_id=session_id,
            actions=("superset_tab_click",),
        )
        figure_actions = (
            "legend_toggle",
            "legend_toggle_activate",
            "legend_toggle_deactivate",
            "chart_click",
            "cross_filter_added",
            "cross_filter_removed",
            "drill_to_detail",
            "drill_by",
            "filter_added",
            "filter_removed",
        )
        figure_event = _fetch_latest_ui_event(
            context.conn,
            dashboard_id=int(dashboard_id),
            session_id=session_id,
            actions=figure_actions,
        )
        log = _fetch_latest_active_chart(context.conn, dashboard_id=int(dashboard_id))

        active_tab = None
        if tab_event and tab_event.get("action") == "superset_tab_click":
            payload = _ui_payload(tab_event)
            active_tab = {
                "id": payload.get("tab_id") or payload.get("tabId"),
                "name": payload.get("tab_name") or payload.get("tabName"),
            }
        if active_tab is None and log and log.get("slice_id"):
            for chart in charts:
                if str(chart.get("slice_id")) == str(log.get("slice_id")):
                    active_tab = chart.get("tab")
                    break
        if active_tab is None:
            try:
                active_tab = fetch_dashboard_default_tab(
                    context.settings, int(dashboard_id)
                )
            except Exception:
                active_tab = None

        def _tab_matches(chart_tab: Any, target_tab: dict[str, Any]) -> bool:
            if not target_tab or not chart_tab:
                return False
            if isinstance(chart_tab, dict):
                if target_tab.get("id") is not None and chart_tab.get("id") is not None:
                    return str(chart_tab.get("id")) == str(target_tab.get("id"))
                if target_tab.get("name") and chart_tab.get("name"):
                    return str(chart_tab.get("name")) == str(target_tab.get("name"))
            return False

        active_charts: list[dict[str, Any]] = []
        if active_tab:
            active_charts = [
                chart
                for chart in charts
                if _tab_matches(chart.get("tab"), active_tab)
            ]

        active_slice_ids: list[int] = []
        for chart in active_charts:
            try:
                active_slice_ids.append(int(chart.get("slice_id")))
            except Exception:
                continue
        chart_filters_by_slice = _fetch_latest_chart_filters_by_slice(
            context.conn,
            slice_ids=active_slice_ids,
            dashboard_id=int(dashboard_id),
        )
        global_filters = _fetch_current_global_filters(
            context.conn,
            dashboard_id=int(dashboard_id),
            session_id=session_id,
        )
        if active_charts:
            for chart in active_charts:
                cross_filters: list[dict[str, Any]] = []
                try:
                    sid = int(chart.get("slice_id"))
                except Exception:
                    sid = None
                if sid is not None:
                    for item in chart_filters_by_slice.get(sid, []):
                        cross_filters.append(item)
                chart["native_filters"] = list(global_filters)
                chart["cross_filters"] = cross_filters

        interaction_payload = None
        interaction_slice_id = None
        interaction_action = None
        if figure_event and figure_event.get("action") in figure_actions:
            interaction_payload = _ui_payload(figure_event)
            interaction_action = figure_event.get("action")
            if isinstance(interaction_action, str) and interaction_action.startswith(
                "legend_toggle"
            ):
                interaction_payload = _normalize_legend_interaction(interaction_payload)
                if interaction_payload is None:
                    interaction_action = None
            interaction_slice_id = _ui_slice_id(figure_event)

        if active_charts:
            for chart in active_charts:
                chart["interaction"] = None
                if interaction_payload and interaction_slice_id is not None:
                    if str(chart.get("slice_id")) == str(interaction_slice_id):
                        chart["interaction"] = {
                            "action": interaction_action,
                            "payload": interaction_payload,
                        }

        return {
            "dashboard_id": int(dashboard_id),
            "active_tab": active_tab,
            "active_charts": active_charts,
        }

    @function_tool
    def get_chart_data_by_id(chart_id: int) -> dict[str, Any]:
        """
        Fetch chart data for a specific chart id using the latest log payload.

        Input:
        - chart_id: Superset slice id (chart id).

        Output:
        - {"chart_id": int, "data": dict} raw Superset chart data response
        - {"error": "..."} if no log payload is found.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if _active_state_masked(context):
            return {"error": "active dashboard state is masked for evaluation"}
        payload = _fetch_latest_chart_log_payload(context.conn, chart_id)
        if not payload:
            return {"error": "no chart log payload found"}
        data = fetch_chart_data_from_log(context.settings, payload)
        return {"chart_id": chart_id, "data": data}

    @function_tool
    def read_dashboard_tools_doc() -> str | dict[str, Any]:
        """Return the dashboard tools documentation markdown."""
        return _read_doc_file(Path("dashboard_tools.md"))

    @function_tool
    def read_schema_explorer_doc() -> str | dict[str, Any]:
        """Return the schema explorer documentation markdown."""
        return _read_doc_file(Path("schema_explorer.md"))

    @function_tool
    def read_semantic_tools_doc() -> str | dict[str, Any]:
        """Return the semantic tools documentation markdown."""
        return _read_doc_file(Path("semantic_tools.md"))

    @function_tool
    def read_charts_doc() -> str | dict[str, Any]:
        """Return the charts documentation markdown."""
        return _read_doc_file(Path("charts.md"))

    @function_tool
    def get_chart_form_data(chart_id: int) -> dict[str, Any]:
        """
        Return Superset chart form_data (parsed from chart params when needed).

        Input:
        - chart_id: Superset slice id (chart id).

        Output:
        - {"chart_id": int, "form_data": dict | null}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            return fetch_chart_form_data(settings, chart_id)
        except Exception as exc:
            return {"error": f"superset chart form_data fetch failed: {exc}"}

    @function_tool
    def get_chart_queries(chart_id: int) -> dict[str, Any]:
        """
        Return Superset chart query_context queries for a chart id.

        Input:
        - chart_id: Superset slice id (chart id).

        Output:
        - {"chart_id": int, "queries": list | null, "form_data": dict | null}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            return fetch_chart_queries(settings, chart_id)
        except Exception as exc:
            return {"error": f"superset chart queries fetch failed: {exc}"}

    @function_tool
    def get_superset_dataset_schema(dataset_id: int) -> dict[str, Any]:
        """
        Return Superset dataset schema metadata for a dataset id.

        Output:
        - {"id": int, "table_name": str, "schema": str | null, "columns": [..]}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            return fetch_dataset_schema(settings, dataset_id)
        except Exception as exc:
            return {"error": f"superset dataset schema fetch failed: {exc}"}

    @function_tool
    def query_superset_dataset(
        query_json: str,
    ) -> dict[str, Any]:
        """
        Query Superset dataset data with filters via /api/v1/chart/data.

        Input:
        - query_json: JSON string for ChartDataRestApi.data payload. Typically includes:
          datasource, queries (columns/metrics/filters/extras/orderby/row_limit),
          result_format, result_type.

        Output:
        - {"data": [...], "raw": {...}}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            return {"error": f"query_json is not valid JSON: {exc}"}
        if not isinstance(query, dict):
            return {"error": "query_json must be a JSON object"}
        try:
            return fetch_dataset_data(settings, query)
        except Exception as exc:
            return {"error": f"superset dataset query failed: {exc}"}

    @function_tool
    def create_cube_view(view_json: str) -> dict[str, Any]:
        """
        Create a semantic view in Cube config using a ViewSpec JSON payload.

        Input:
        - view_json: JSON string that matches ViewSpec.

        Output:
        - {"status": "created|updated", "view_name": "...", "view_file": "...",
           "cube_reload_status": "ok|skipped|error", "physical_name": "...", "warnings": [...]}
        - {"error": "..."} on validation or config issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            payload = json.loads(view_json)
        except json.JSONDecodeError as exc:
            return {"error": f"view_json is not valid JSON: {exc}"}
        try:
            view_spec = ViewSpec.model_validate(payload)
        except Exception as exc:
            return {"error": f"view spec invalid: {exc}"}
        try:
            result = semantic_create_view(settings, view_spec)
        except Exception as exc:
            context = _get_active_context()
            if context and context.conn:
                log_unified_event(
                    context.conn,
                    "semantic_view_create_failed",
                    {"view_name": view_spec.view_name, "error": str(exc)},
                )
            return {"error": f"create view failed: {exc}"}
        context = _get_active_context()
        if context and context.conn:
            log_create_view(context.conn, view_spec, result)
        return result.model_dump()

    @function_tool
    def sync_superset_dataset(request_json: str) -> dict[str, Any]:
        """
        Create or refresh a Superset dataset for a table/view.

        Input:
        - request_json: JSON string matching SupersetDatasetSyncRequest.

        Output:
        - {"status": "created|updated", "dataset_id": int|None, "created": bool,
           "updated": bool, "warnings": [...]}
        - {"error": "..."} on validation or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            payload = json.loads(request_json)
        except json.JSONDecodeError as exc:
            return {"error": f"request_json is not valid JSON: {exc}"}
        try:
            request = SupersetDatasetSyncRequest.model_validate(payload)
        except Exception as exc:
            return {"error": f"sync request invalid: {exc}"}
        effective_settings = settings
        if request.superset_username and request.superset_password:
            effective_settings = replace(
                settings,
                superset_username=request.superset_username,
                superset_password=request.superset_password,
            )
        try:
            result = semantic_sync_dataset(effective_settings, request)
        except Exception as exc:
            context = _get_active_context()
            if context and context.conn:
                log_unified_event(
                    context.conn,
                    "superset_dataset_sync_failed",
                    {"table_name": request.table_name, "error": str(exc)},
                )
            return {"error": f"superset dataset sync failed: {exc}"}
        context = _get_active_context()
        if context and context.conn:
            log_dataset_sync(context.conn, request, result)
        return result.model_dump()

    @function_tool
    def create_semantic_view_and_dataset(request_json: str) -> dict[str, Any]:
        """
        Create semantic view and sync Superset dataset in one call, then return chart-ready hints.

        Input JSON shape:
        - {"view": <ViewSpec>, "dataset": <SupersetDatasetSyncRequest-like>}
          or {"view_json": "...", "dataset_json": "..."}.
        - dataset.table_name defaults to view.view_name when omitted.
        - dataset can also be omitted if view.superset_sync exists.

        Output:
        - {"status":"ok","view":{...},"dataset":{...},"chart_seed":{...},"warnings":[...]}
        - {"error":"..."} on validation or API errors.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            payload = json.loads(request_json)
        except json.JSONDecodeError as exc:
            return {"error": f"request_json is not valid JSON: {exc}"}
        if not isinstance(payload, dict):
            return {"error": "request_json must be a JSON object"}

        view_payload: Any = payload.get("view")
        if view_payload is None and isinstance(payload.get("view_json"), str):
            try:
                view_payload = json.loads(payload["view_json"])
            except json.JSONDecodeError as exc:
                return {"error": f"view_json is not valid JSON: {exc}"}
        if view_payload is None:
            # Backward-friendly mode: treat top-level as ViewSpec when "view" key is omitted.
            view_payload = payload

        try:
            view_spec = ViewSpec.model_validate(view_payload)
        except Exception as exc:
            return {"error": f"view spec invalid: {exc}"}

        try:
            view_result = semantic_create_view(settings, view_spec)
        except Exception as exc:
            context = _get_active_context()
            if context and context.conn:
                log_unified_event(
                    context.conn,
                    "semantic_view_create_failed",
                    {"view_name": view_spec.view_name, "error": str(exc)},
                )
            return {"error": f"create view failed: {exc}"}

        dataset_payload: Any = payload.get("dataset")
        if dataset_payload is None and isinstance(payload.get("dataset_json"), str):
            try:
                dataset_payload = json.loads(payload["dataset_json"])
            except json.JSONDecodeError as exc:
                return {"error": f"dataset_json is not valid JSON: {exc}"}
        if dataset_payload is None and isinstance(view_spec.superset_sync, dict):
            dataset_payload = dict(view_spec.superset_sync)
        if not isinstance(dataset_payload, dict):
            return {
                "error": "dataset payload missing; provide dataset/database_id or view.superset_sync",
                "view": view_result.model_dump(),
            }

        if not dataset_payload.get("table_name"):
            dataset_payload["table_name"] = view_spec.view_name
        if not dataset_payload.get("schema") and not dataset_payload.get("schema_name"):
            dataset_payload["schema"] = "public"
        if payload.get("superset_username") and not dataset_payload.get("superset_username"):
            dataset_payload["superset_username"] = payload.get("superset_username")
        if payload.get("superset_password") and not dataset_payload.get("superset_password"):
            dataset_payload["superset_password"] = payload.get("superset_password")

        try:
            sync_request = SupersetDatasetSyncRequest.model_validate(dataset_payload)
        except Exception as exc:
            return {
                "error": f"sync request invalid: {exc}",
                "view": view_result.model_dump(),
            }

        effective_settings = settings
        if sync_request.superset_username and sync_request.superset_password:
            effective_settings = replace(
                settings,
                superset_username=sync_request.superset_username,
                superset_password=sync_request.superset_password,
            )
        try:
            dataset_result = semantic_sync_dataset(effective_settings, sync_request)
        except Exception as exc:
            context = _get_active_context()
            if context and context.conn:
                log_unified_event(
                    context.conn,
                    "superset_dataset_sync_failed",
                    {"table_name": sync_request.table_name, "error": str(exc)},
                )
            return {
                "error": f"superset dataset sync failed: {exc}",
                "view": view_result.model_dump(),
            }

        chart_seed = None
        if dataset_result.dataset_id:
            try:
                schema = fetch_dataset_schema(effective_settings, int(dataset_result.dataset_id))
                columns = schema.get("columns") or []
                if isinstance(columns, list):
                    chart_seed = _infer_chart_seed_from_columns(
                        int(dataset_result.dataset_id),
                        view_spec.view_name,
                        columns,
                    )
            except Exception:
                chart_seed = {
                    "dataset_id": int(dataset_result.dataset_id),
                    "datasource_type": "table",
                }

        context = _get_active_context()
        if context and context.conn:
            log_create_view(context.conn, view_spec, view_result)
            log_dataset_sync(context.conn, sync_request, dataset_result)

        warnings = list(view_result.warnings or []) + list(dataset_result.warnings or [])
        if dataset_result.dataset_id is None:
            warnings.append("dataset_id is null; cannot produce chart seed")

        return {
            "status": "ok",
            "view": view_result.model_dump(),
            "dataset": dataset_result.model_dump(),
            "chart_seed": chart_seed,
            "warnings": warnings,
        }

    @function_tool
    def delete_cube_view(view_name: str) -> dict[str, Any]:
        """
        Delete a semantic view from Cube config and reload metadata.

        Input:
        - view_name: view name to delete.

        Output:
        - {"status": "deleted", "view_name": "...", "view_file": "...", "cube_reload_status": "...", "warnings": [...]}
        - {"error": "..."} on failure.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            result = semantic_delete_view(settings, view_name)
        except Exception as exc:
            context = _get_active_context()
            if context and context.conn:
                log_unified_event(
                    context.conn,
                    "semantic_view_delete_failed",
                    {"view_name": view_name, "error": str(exc)},
                )
            return {"error": f"delete view failed: {exc}"}
        context = _get_active_context()
        if context and context.conn:
            log_unified_event(
                context.conn,
                "semantic_view_deleted",
                {"view_name": view_name, "view_file": result.view_file},
            )
        return result.model_dump()

    @function_tool
    def list_cube_tables() -> list[dict[str, Any]] | dict[str, Any]:
        """
        List cubes and views available from Cube configuration or meta.

        Output:
        - [{"name": "...", "type": "cube|view"}]
        """
        settings, error = _get_active_settings()
        if error:
            return error

        entries: dict[str, dict[str, Any]] = {}
        if settings.cube_conf_path:
            schema = load_repo_schema(settings.cube_conf_path)
            for name, info in schema.items():
                entry_type = info.get("type") or "cube"
                entries[name] = {"name": name, "type": entry_type}

        if settings.cube_rest_url:
            meta = fetch_cube_meta(settings)
            for cube in meta.get("cubes", []) or []:
                if isinstance(cube, dict) and cube.get("name"):
                    name = cube["name"]
                    entries.setdefault(name, {"name": name, "type": "cube"})

        if not entries:
            return {"error": "cube configuration and meta not available"}
        return sorted(entries.values(), key=lambda item: (item.get("type"), item.get("name")))

    @function_tool
    def get_cube_schema(table_name: str) -> dict[str, Any]:
        """
        Return schema details for a cube or view.

        Output (conf-based):
        - {"name": "...", "type": "cube|view", "columns": [...], "sql_table": "...", "joined": {...}}

        Output (meta-based cube):
        - {"name": "...", "type": "cube", "measures": [...], "dimensions": [...], "segments": [...]}
        """
        settings, error = _get_active_settings()
        if error:
            return error

        if settings.cube_conf_path:
            schema = load_repo_schema(settings.cube_conf_path)
            entry = schema.get(table_name)
            if entry:
                entry_type = entry.get("type") or "cube"
                return {"name": table_name, "type": entry_type, **entry}

        if settings.cube_rest_url:
            meta = fetch_cube_meta(settings)
            for cube in meta.get("cubes", []) or []:
                if isinstance(cube, dict) and cube.get("name") == table_name:
                    measures = [m.get("name") for m in (cube.get("measures") or []) if m.get("name")]
                    dimensions = [d.get("name") for d in (cube.get("dimensions") or []) if d.get("name")]
                    segments = [s.get("name") for s in (cube.get("segments") or []) if s.get("name")]
                    joins = list((cube.get("joins") or {}).keys()) if isinstance(cube.get("joins"), dict) else []
                    return {
                        "name": table_name,
                        "type": "cube",
                        "measures": measures,
                        "dimensions": dimensions,
                        "segments": segments,
                        "joins": joins,
                    }

        return {"error": f"cube or view '{table_name}' not found"}

    @function_tool
    def query_cube(query_json: str) -> dict[str, Any]:
        """
        Run a Cube REST API query (/cubejs-api/v1/load).

        Input:
        - query_json: JSON string for a Cube query object (or list for data blending).

        Output:
        - Raw Cube response payload.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            return {"error": f"query_json is not valid JSON: {exc}"}
        if not isinstance(query, (dict, list)):
            return {"error": "query_json must be a JSON object or list"}
        try:
            return run_cube_query(settings, query)
        except Exception as exc:
            return {"error": f"cube query failed: {exc}"}

    @function_tool
    def get_semantic_schema(
        type: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """
        Return Cube meta schema with filtered keys.

        Input:
        - type: "cubes" or "views" to filter output.
        - name: optional substring filter on cube/view name.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        if not settings.cube_rest_url:
            return {"error": "CUBE_REST_URL not configured"}
        try:
            payload = fetch_cube_meta(settings)
        except Exception as exc:
            return {"error": f"cube meta fetch failed: {exc}"}
        if not isinstance(payload, dict):
            return payload
        drop_keys = {
            "suggestFilterValues",
            "isVisible",
            "public",
            "segments",
            "hierarchies",
            "folders",
            "nestedFolders",
            "drillMembers",
            "drillMembersGrouped",
            "connectedComponent",
        }

        def _strip_keys(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {
                    key: _strip_keys(value)
                    for key, value in obj.items()
                    if key not in drop_keys
                }
            if isinstance(obj, list):
                return [_strip_keys(item) for item in obj]
            return obj

        cubes = payload.get("cubes")
        if not isinstance(cubes, list):
            return payload
        filtered: list[dict[str, Any]] = []
        name_filter = name.lower() if isinstance(name, str) else None
        for cube in cubes:
            if not isinstance(cube, dict):
                continue
            cube_name = str(cube.get("name") or "")
            if name_filter and name_filter not in cube_name.lower():
                continue
            entry = _strip_keys(cube)
            is_view = cube_name.startswith("view_") or entry.get("type") == "view"
            if type == "views" and not is_view:
                continue
            if type == "cubes" and is_view:
                continue
            filtered.append(entry)
        if type in ("views", "cubes"):
            return {type: filtered}
        return {"cubes": filtered}

    @function_tool
    def list_superset_databases_meta() -> dict[str, Any]:
        """
        Return Superset database ids and names.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        if not settings.superset_username or not settings.superset_password:
            return {"error": "Superset credentials not configured"}
        if not (settings.superset_internal_url or settings.superset_public_url):
            return {"error": "Superset URL not configured"}
        try:
            session = _api_session_with_bearer(settings)
            base_url = _get_base_url(settings)
            _ensure_csrf(session, base_url)
            response = session.get(
                f"{base_url}/api/v1/database/",
                params={"q": "(page:0,page_size:200)"},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            return {"error": f"superset database meta fetch failed: {exc}"}

        result = payload.get("result")
        items: list[dict[str, Any]] = []
        if isinstance(result, list):
            for entry in result:
                if not isinstance(entry, dict):
                    continue
                db_id = entry.get("id") or entry.get("database_id")
                name = entry.get("database_name") or entry.get("name")
                if db_id is None or not name:
                    continue
                items.append({"id": db_id, "database_name": name})
        return {"count": len(items), "result": items}

    @function_tool
    def list_superset_database_tables(
        database_id: int,
        schema_name: str = "public",
        limit: int = 500,
    ) -> dict[str, Any]:
        """
        Return table names for a Superset database schema.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        if not settings.superset_username or not settings.superset_password:
            return {"error": "Superset credentials not configured"}
        if not (settings.superset_internal_url or settings.superset_public_url):
            return {"error": "Superset URL not configured"}
        try:
            session = _api_session_with_bearer(settings)
            base_url = _get_base_url(settings)
        except Exception as exc:
            return {"error": f"superset api session failed: {exc}"}

        page = 0
        page_size = 200
        remaining = max(1, limit)
        results: list[str] = []
        while remaining > 0:
            response = session.get(
                f"{base_url}/api/v1/database/{database_id}/tables/",
                params={
                    "q": f"(schema_name:{schema_name},page:{page},page_size:{page_size})"
                },
                timeout=30,
            )
            if response.status_code >= 400:
                return {"error": f"superset api error: {response.text}"}
            payload = response.json()
            batch = payload.get("result")
            if not isinstance(batch, list) or not batch:
                break
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                name = (
                    entry.get("name")
                    or entry.get("value")
                    or entry.get("table")
                    or entry.get("table_name")
                )
                if isinstance(name, str):
                    results.append(name)
                    remaining -= 1
                    if remaining <= 0:
                        break
            if len(batch) < page_size:
                break
            page += 1
        return {"result": results}

    @function_tool
    def list_superset_datasets(
        user_id: int | None = None,
        username: str | None = None,
        name: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """
        List Superset datasets filtered by user or name.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        if not settings.superset_username or not settings.superset_password:
            return {"error": "Superset credentials not configured"}
        if not (settings.superset_internal_url or settings.superset_public_url):
            return {"error": "Superset URL not configured"}
        if username and user_id:
            return {"error": "Provide either user_id or username, not both"}
        if username and not user_id:
            meta_db_uri = settings.superset_meta_db_uri
            if not meta_db_uri:
                return {"error": "Superset meta DB not configured"}
            try:
                user_id = lookup_user_id(meta_db_uri, username)
            except Exception as exc:
                return {"error": f"Superset API error: {exc}"}
            if not user_id:
                return {"count": 0, "result": []}

        try:
            session = _api_session_with_bearer(settings)
            base_url = _get_base_url(settings)
            _ensure_csrf(session, base_url)
        except Exception as exc:
            return {"error": f"Superset API error: {exc}"}

        page = 0
        page_size = 100
        remaining = max(1, limit)
        results: list[dict[str, Any]] = []
        name_filter = name.strip().lower() if isinstance(name, str) else None

        while remaining > 0:
            response = session.get(
                f"{base_url}/api/v1/dataset/",
                params={"page": page, "page_size": page_size},
                timeout=30,
            )
            if response.status_code >= 400:
                return {"error": f"Superset API error: {response.text}"}
            payload = response.json()
            batch = payload.get("result")
            if not isinstance(batch, list) or not batch:
                break
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                if user_id is not None:
                    owners = entry.get("owners") or []
                    owner_ids = [
                        o.get("id") for o in owners if isinstance(o, dict) and o.get("id")
                    ]
                    if user_id not in owner_ids:
                        continue
                if name_filter:
                    table_name = str(entry.get("table_name") or "").lower()
                    dataset_name = str(entry.get("dataset_name") or entry.get("name") or "").lower()
                    if name_filter not in table_name and name_filter not in dataset_name:
                        continue
                results.append(
                    {
                        "id": entry.get("id") or entry.get("dataset_id"),
                        "table_name": entry.get("table_name"),
                        "dataset_name": entry.get("dataset_name") or entry.get("name"),
                        "database": entry.get("database"),
                    }
                )
                remaining -= 1
                if remaining <= 0:
                    break
            if len(batch) < page_size:
                break
            page += 1
        return {"count": len(results), "result": results}

    @function_tool
    def list_chart_templates(viz_type: str | None = None) -> dict[str, Any]:
        """
        Return chart template definitions.
        """
        templates = _load_chart_templates()
        if not templates:
            return {"templates": []}
        if viz_type:
            entry = templates.get(viz_type)
            if entry is None:
                return {"error": f"viz_type not found: {viz_type}"}
            return {"templates": {viz_type: entry}}
        return {"templates": templates}

    @function_tool
    def create_superset_chart(request_json: str) -> dict[str, Any]:
        """
        Create a Superset chart using templates.

        Input:
        - request_json: JSON string matching ChartCreateRequest.

        Output:
        - {"status": "created", "chart_id": int | null, "slice_name": str, "warnings": [...]}
        - {"error": "..."} on failure.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        if not settings.superset_username or not settings.superset_password:
            return {"error": "Superset credentials not configured"}
        if not (settings.superset_internal_url or settings.superset_public_url):
            return {"error": "Superset URL not configured"}

        try:
            payload = json.loads(request_json)
        except json.JSONDecodeError as exc:
            return {"error": f"request_json is not valid JSON: {exc}"}
        if not isinstance(payload, dict):
            return {"error": "request_json must be a JSON object"}

        normalization_warnings: list[str] = []
        viz_aliases = {
            "echarts_timeseries_line": "line",
            "echarts_timeseries_bar": "bar",
            "echarts_timeseries_scatter": "scatter",
        }

        raw_viz_type = payload.get("viz_type")
        if isinstance(raw_viz_type, str):
            mapped_viz = viz_aliases.get(raw_viz_type.strip())
            if mapped_viz and mapped_viz != raw_viz_type:
                payload["viz_type"] = mapped_viz
                normalization_warnings.append(
                    f"normalized viz_type from {raw_viz_type} to {mapped_viz}"
                )

        datasource = payload.get("datasource")
        if payload.get("dataset_id") in (None, "") and isinstance(datasource, dict):
            ds_id = datasource.get("id") or datasource.get("datasource_id")
            if ds_id not in (None, ""):
                payload["dataset_id"] = ds_id
                normalization_warnings.append("inferred dataset_id from datasource.id")
            ds_type = datasource.get("type")
            if ds_type and not payload.get("datasource_type"):
                payload["datasource_type"] = ds_type

        # Backward compatibility: convert legacy form_data payload to encodings/options.
        if "encodings" not in payload and isinstance(payload.get("form_data"), dict):
            form_data = payload.get("form_data") or {}
            encodings: dict[str, Any] = {}
            options: dict[str, Any] = {}
            time_col = (
                form_data.get("time_column")
                or form_data.get("granularity_sqla")
                or form_data.get("granularity")
            )
            if time_col:
                encodings["time_column"] = time_col
            groupby = form_data.get("groupby")
            if not groupby and isinstance(form_data.get("columns"), list):
                groupby = form_data.get("columns")
            if isinstance(groupby, list):
                encodings["groupby"] = groupby
            if isinstance(form_data.get("metrics"), list):
                encodings["metrics"] = form_data.get("metrics")

            for key in (
                "granularity_sqla",
                "time_grain_sqla",
                "time_range",
                "adhoc_filters",
                "filters",
                "all_columns",
                "row_limit",
                "orderby",
                "order_desc",
            ):
                if key in form_data:
                    options[key] = form_data.get(key)

            payload["encodings"] = encodings
            payload["options"] = options
            normalization_warnings.append("normalized legacy form_data into encodings/options")

        try:
            request = ChartCreateRequest.model_validate(payload)
        except Exception as exc:
            return {"error": f"chart request invalid: {exc}"}

        templates = _load_chart_templates()
        template = templates.get(request.viz_type)
        if not template:
            return {"error": f"Unsupported viz_type: {request.viz_type}"}

        actual_viz_type = template.get("viz_type") or request.viz_type
        form_data = dict(template.get("form_data") or {})
        form_data["viz_type"] = actual_viz_type
        form_data["datasource"] = f"{request.dataset_id}__{request.datasource_type}"

        enc = request.encodings or {}
        opts = request.options or {}
        warnings: list[str] = list(normalization_warnings)

        def _normalize_metrics(value: Any) -> Any:
            if not isinstance(value, list):
                return value
            normalized: list[Any] = []
            for metric in value:
                if isinstance(metric, dict):
                    normalized.append(metric)
                    continue
                if isinstance(metric, str):
                    text = metric.strip()
                    match = re.match(r"^([A-Z]+)\\((.+)\\)$", text)
                    if match:
                        agg = match.group(1)
                        col = match.group(2).strip()
                        normalized.append(
                            {
                                "expressionType": "SIMPLE",
                                "aggregate": agg,
                                "column": {"column_name": col},
                                "label": f"{agg}({col})",
                            }
                        )
                    else:
                        normalized.append(metric)
                    continue
                normalized.append(metric)
            return normalized

        def _set_if_present(key: str, target_key: str | None = None) -> None:
            if key in enc:
                form_data[target_key or key] = enc.get(key)
            if key in opts:
                form_data[target_key or key] = opts.get(key)

        if "time_column" in enc:
            form_data["granularity_sqla"] = enc.get("time_column")
        _set_if_present("granularity_sqla")
        _set_if_present("time_grain_sqla")
        _set_if_present("time_range")
        _set_if_present("groupby")
        if "metrics" in enc:
            form_data["metrics"] = _normalize_metrics(enc.get("metrics"))
        elif "metrics" in opts:
            form_data["metrics"] = _normalize_metrics(opts.get("metrics"))
        _set_if_present("adhoc_filters")
        _set_if_present("filters")
        _set_if_present("all_columns")
        _set_if_present("row_limit")
        _set_if_present("orderby")
        _set_if_present("order_desc")

        if actual_viz_type == "pie":
            if "metric" in enc:
                metric_value = enc.get("metric")
                if isinstance(metric_value, str):
                    match = re.match(r"^([A-Z]+)\\((.+)\\)$", metric_value.strip())
                    if match:
                        agg = match.group(1)
                        col = match.group(2).strip()
                        metric_value = {
                            "expressionType": "SIMPLE",
                            "aggregate": agg,
                            "column": {"column_name": col},
                            "label": f"{agg}({col})",
                        }
                form_data["metric"] = metric_value
            elif "metrics" in enc and isinstance(enc.get("metrics"), list):
                metrics = enc.get("metrics") or []
                form_data["metric"] = metrics[0] if metrics else None
            if not form_data.get("metric"):
                return {"error": "pie requires metric or metrics"}
            if not form_data.get("groupby"):
                return {"error": "pie requires groupby"}

        if actual_viz_type == "table":
            if form_data.get("all_columns"):
                form_data["query_mode"] = "raw"
                form_data.pop("metrics", None)
                form_data.pop("groupby", None)
            else:
                form_data["query_mode"] = "aggregate"
                if not form_data.get("metrics"):
                    return {"error": "table aggregate requires metrics or all_columns"}

        if actual_viz_type in {
            "echarts_timeseries_line",
            "echarts_timeseries_bar",
            "echarts_timeseries_scatter",
        }:
            if not form_data.get("granularity_sqla"):
                return {
                    "error": f"{actual_viz_type} requires granularity_sqla or time_column"
                }
            if not form_data.get("metrics"):
                return {"error": f"{actual_viz_type} requires metrics"}

        chart_payload = {
            "slice_name": request.slice_name,
            "viz_type": actual_viz_type,
            "datasource_id": request.dataset_id,
            "datasource_type": request.datasource_type,
            "params": json.dumps(form_data, ensure_ascii=False),
        }
        if request.owners is not None:
            chart_payload["owners"] = request.owners
        if request.dashboard_id is not None:
            chart_payload["dashboards"] = [request.dashboard_id]

        try:
            session = _api_session_with_bearer(settings)
            base_url = _get_base_url(settings)
            _ensure_csrf(session, base_url)
            response = session.post(
                f"{base_url}/api/v1/chart/",
                json=chart_payload,
                timeout=30,
            )
            if response.status_code >= 400:
                return {"error": f"Superset API error: {response.text}"}
            result = response.json()
        except Exception as exc:
            return {"error": f"Superset API error: {exc}"}

        def _coerce_int(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def _extract_chart_id(payload_obj: Any, depth: int = 0) -> int | None:
            if depth > 6:
                return None
            if isinstance(payload_obj, dict):
                for key in ("id", "slice_id", "chart_id", "sliceId", "chartId", "pk"):
                    candidate = _coerce_int(payload_obj.get(key))
                    if candidate is not None:
                        return candidate
                for child in payload_obj.values():
                    found = _extract_chart_id(child, depth + 1)
                    if found is not None:
                        return found
            elif isinstance(payload_obj, list):
                for child in payload_obj:
                    found = _extract_chart_id(child, depth + 1)
                    if found is not None:
                        return found
            return None

        chart_id = _extract_chart_id(result)

        if chart_id is None:
            # Some Superset versions create chart successfully without returning id.
            # Fallback: list charts and recover id by (slice_name, datasource_id, datasource_type).
            page = 0
            page_size = 100
            candidates: list[int] = []
            while page < 10:
                lookup_resp = session.get(
                    f"{base_url}/api/v1/chart/",
                    params={"q": f"(page:{page},page_size:{page_size})"},
                    timeout=30,
                )
                if lookup_resp.status_code >= 400:
                    warnings.append(
                        f"chart lookup failed {lookup_resp.status_code}: {lookup_resp.text}"
                    )
                    break
                lookup_payload = lookup_resp.json()
                batch = lookup_payload.get("result") if isinstance(lookup_payload, dict) else []
                if not isinstance(batch, list) or not batch:
                    break
                for entry in batch:
                    if not isinstance(entry, dict):
                        continue
                    entry_name = entry.get("slice_name") or entry.get("name")
                    if str(entry_name or "") != request.slice_name:
                        continue
                    entry_ds_id = _coerce_int(entry.get("datasource_id"))
                    entry_ds_type = str(entry.get("datasource_type") or "table")
                    if entry_ds_id != request.dataset_id:
                        continue
                    if entry_ds_type != request.datasource_type:
                        continue
                    found_id = _coerce_int(
                        entry.get("id")
                        or entry.get("slice_id")
                        or entry.get("chart_id")
                    )
                    if found_id is not None:
                        candidates.append(found_id)
                if len(batch) < page_size:
                    break
                page += 1
            if candidates:
                chart_id = max(candidates)
                warnings.append("chart id recovered via chart lookup")
            else:
                warnings.append("chart id not returned from Superset")

        return {
            "status": "created",
            "chart_id": chart_id,
            "slice_id": chart_id,
            "slice_name": request.slice_name,
            "warnings": warnings,
        }

    @function_tool
    def append_chart_to_dashboard(request_json: str) -> dict[str, Any]:
        """
        Append an existing chart to a dashboard layout (position_json).

        Input:
        - request_json: JSON string with
          { "dashboard_id": int, "chart_id": int, "tab_id"?: str, "tab_name"?: str,
            "width"?: int, "height"?: int }.

        Output:
        - {"status":"appended|already_exists","dashboard_id":int,"chart_id":int,
           "container_id":str|null,"row_id":str|null,"chart_node_id":str|null,"tab":{...}|null}
        - {"error":"..."} on validation/API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        if not settings.superset_username or not settings.superset_password:
            return {"error": "Superset credentials not configured"}
        if not (settings.superset_internal_url or settings.superset_public_url):
            return {"error": "Superset URL not configured"}

        try:
            payload = json.loads(request_json)
        except json.JSONDecodeError as exc:
            return {"error": f"request_json is not valid JSON: {exc}"}
        if not isinstance(payload, dict):
            return {"error": "request_json must be a JSON object"}

        dashboard_id = payload.get("dashboard_id")
        if dashboard_id is None:
            return {"error": "dashboard_id is required"}
        try:
            dashboard_id = int(dashboard_id)
        except (TypeError, ValueError):
            return {"error": "dashboard_id must be an integer"}

        append_payload = dict(payload)
        append_payload.pop("dashboard_id", None)
        try:
            request = DashboardAppendChartRequest.model_validate(append_payload)
        except Exception as exc:
            return {"error": f"append request invalid: {exc}"}

        try:
            return append_chart_to_dashboard_layout(
                settings=settings,
                dashboard_id=dashboard_id,
                chart_id=request.chart_id,
                tab_id=request.tab_id,
                tab_name=request.tab_name,
                width=request.width,
                height=request.height,
            )
        except Exception as exc:
            return {"error": f"Superset API error: {exc}"}

    @function_tool
    def get_facts() -> list[str] | dict[str, Any]:
        """
        Return the list of fact tables in the configured star schema.

        Example:
        - ["fact_sales"]
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.get_facts()

    @function_tool
    def get_schema_info(fact_table: str) -> list[dict] | dict[str, Any]:
        """
        Return the schema graph nodes for a fact table.

        Each node includes `name` and `type` ("fact" or "dimension").
        Dimension nodes include `attributes`; the fact node includes `measures` and `fks`.

        Example:
        - [{"name": "fact_sales", "type": "fact", "measures": [...], "fks": [...]},
           {"name": "dim_date", "type": "dimension", "attributes": [...]}, ...]
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.get_schema(fact_table)

    @function_tool
    def search_attribute(fact_table: str, attribute_name: str) -> list[dict] | dict[str, Any]:
        """
        Return hierarchy paths that lead to the attribute for the fact schema.

        Each result includes: dimension, attribute, path (dimension/level/attribute steps),
        and stats (count, distinct_count, min/max, dtype, unique_values metadata).
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.search_attribute(fact_table, attribute_name)

    @function_tool
    def search_value_exists(
        fact_table: str,
        attribute_name: str,
        value: str,
    ) -> bool | dict[str, Any]:
        """
        Return whether the attribute value exists for the given fact table and attribute.

        Example:
        - search_value_exists("fact_sales", "year", "2024") -> True
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.search_value(fact_table, attribute_name, value)

EXAMPLE = """
Example query_json
** For metrics: use the `aggregate(column_name)` format for labels **
```json
{
  "datasource": { "id": 28, "type": "table" },
  "queries": [
    {
      "columns": ["dim_product_department", "dim_product_category"],
      "metrics": [
        {
          "expressionType": "SIMPLE",
          "aggregate": "SUM",
          "column": { "column_name": "previous_units" },
          "label": "SUM(previous_units)"
        },
        {
          "expressionType": "SIMPLE",
          "aggregate": "SUM",
          "column": { "column_name": "total_units_sold" },
          "label": "SUM(total_units_sold)"
        },
        {
          "expressionType": "SIMPLE",
          "aggregate": "MAX",
          "column": { "column_name": "qoq_growth_rate" },
          "label": "MAX(qoq_growth_rate)"
        }
      ],
      "filters": [],
      "extras": {
        "where": "dim_date_quarter_start >= CAST('2024-07-01 00:00:00' AS TIMESTAMP)",
        "having": ""
      },
      "orderby": [["MAX(qoq_growth_rate)", true]],
      "row_limit": 10000
    }
  ],
  "result_format": "json",
  "result_type": "full"
}
"""

class AgentRunner:
    def __init__(self) -> None:
        self._orchestrator_agent = None
        self._context_resolver_agent = None
        self._chart_manager_agent = None
        self._schema_explorer_agent = None
        self._answer_composer_agent = None
        self._docs_retriever_agent = None
        self._insight_seeker_agent = None
        self._init_lock = asyncio.Lock()
        self._init_error: Exception | None = None
        self._initialized = False
        self._dashboard_tools_doc: str | None = None
        self._model_name = os.getenv("AGENT_MODEL", "gpt-5-mini")
        self._max_turns = self._positive_int_env("AGENT_MAX_TURNS", 6)
        self._timeout_sec = self._positive_int_env("AGENT_TIMEOUT_SEC", 90)

    @staticmethod
    def _positive_int_env(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    @property
    def available(self) -> bool:
        return self._orchestrator_agent is not None

    async def startup(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            try:
                agents = self._build_agents(model_name=self._model_name)
                self._orchestrator_agent = agents.get("orchestrator")
                self._context_resolver_agent = agents.get("context_resolver")
                self._chart_manager_agent = agents.get("chart_manager")
                self._schema_explorer_agent = agents.get("schema_explorer")
                self._answer_composer_agent = agents.get("answer_composer")
                self._docs_retriever_agent = agents.get("docs_retriever")
                self._insight_seeker_agent = agents.get("insight_seeker")
                self._dashboard_tools_doc = self._load_dashboard_tools_doc()
            except Exception as exc:  # pragma: no cover - defensive init guard
                self._orchestrator_agent = None
                self._context_resolver_agent = None
                self._chart_manager_agent = None
                self._schema_explorer_agent = None
                self._answer_composer_agent = None
                self._docs_retriever_agent = None
                self._insight_seeker_agent = None
                self._init_error = exc
            self._initialized = True

    async def _ensure_model(self, model_name: str | None) -> None:
        desired_model = (model_name or os.getenv("AGENT_MODEL", "gpt-5-mini")).strip()
        if not desired_model:
            desired_model = "gpt-5-mini"
        await self.startup()
        if self._orchestrator_agent is not None and self._model_name == desired_model:
            return
        async with self._init_lock:
            if self._orchestrator_agent is not None and self._model_name == desired_model:
                return
            agents = self._build_agents(model_name=desired_model)
            self._orchestrator_agent = agents.get("orchestrator")
            self._context_resolver_agent = agents.get("context_resolver")
            self._chart_manager_agent = agents.get("chart_manager")
            self._schema_explorer_agent = agents.get("schema_explorer")
            self._answer_composer_agent = agents.get("answer_composer")
            self._docs_retriever_agent = agents.get("docs_retriever")
            self._insight_seeker_agent = agents.get("insight_seeker")
            self._model_name = desired_model

    def _build_agents(self, model_name: str | None = None) -> dict[str, Any]:
        if Agent is None or ModelSettings is None:
            return {
                "orchestrator": None,
                "context_resolver": None,
                "chart_manager": None,
                "schema_explorer": None,
                "answer_composer": None,
                "docs_retriever": None,
                "insight_seeker": None,
            }

        selected_model = (model_name or os.getenv("AGENT_MODEL", "gpt-5-mini")).strip()
        if not selected_model:
            selected_model = "gpt-5-mini"

        service_tier = os.getenv("OPENAI_SERVICE_TIER", "flex").strip()
        extra_args = {"service_tier": service_tier} if service_tier else None
        reasoning_effort = os.getenv("AGENT_REASONING_EFFORT", "medium").strip().lower()
        if reasoning_effort not in {"low", "medium", "high"}:
            reasoning_effort = "medium"

        model_settings = ModelSettings(
            reasoning={"effort": reasoning_effort},
            verbosity="low",
            max_turns=100,
            response_format={"type": "json_object", "schema": Answer.model_json_schema()},
            extra_args=extra_args,
        )
        insight_seeker_agent = Agent(
            name="InsightSeeker Agent",
            model=selected_model,
            tools=[],
            model_settings=model_settings,
            instructions=prompts.INSIGHT_SEEKER_AGENT,
        )

        context_resolver_agent = Agent(
            name="Context Resolver Agent",
            model=selected_model,
            tools=[],
            model_settings=model_settings,
            instructions=prompts.CONTEXT_RESOLVER_AGENT,
        )

        chart_manager_agent = Agent(
            name="ChartManager Agent",
            model=selected_model,
            tools=[
                get_active_chart_log,
                get_chart_sql,
                get_active_tab_charts,
                list_dashboard_charts,
                get_dashboard_layout,
                list_superset_datasets,
                get_chart_form_data,
                get_chart_queries,
                get_superset_dataset_schema,
                query_superset_dataset,
                get_active_chart_data,
                get_chart_data_by_id,
                query_cube,
                list_chart_templates,
                list_superset_databases_meta,
                list_superset_database_tables,
                create_semantic_view_and_dataset,
                create_superset_chart,
                append_chart_to_dashboard,
            ]
            if function_tool
            else [],
            model_settings=model_settings,
            instructions=prompts.CHART_MANAGER_AGENT,
        )

        schema_explorer_agent = Agent(
            name="SchemaExplorer Agent",
            model=selected_model,
            tools=[
                get_facts,
                get_schema_info,
                search_attribute,
                search_value_exists,
                list_cube_tables,
                get_cube_schema,
                get_semantic_schema,
            ]
            if function_tool
            else [],
            model_settings=model_settings,
            instructions=prompts.SCHEMA_EXPLORER_AGENT,
        )

        answer_composer_agent = Agent(
            name="Answer Composer Agent",
            model=selected_model,
            tools=[],
            model_settings=model_settings,
            instructions=prompts.ANSWER_COMPOSER_AGENT,
        )

        docs_retriever_agent = Agent(
            name="DocsRetriever Agent",
            model=selected_model,
            tools=[
                read_dashboard_tools_doc,
                read_schema_explorer_doc,
                read_semantic_tools_doc,
                read_charts_doc,
            ]
            if function_tool
            else [],
            model_settings=model_settings,
            instructions=prompts.DOCS_RETRIEVER_AGENT,
        )

        if function_tool:
            @function_tool
            async def run_context_resolver_agent(payload_json: str) -> str:
                """Resolve the question against Live State and AER candidates."""
                enriched_payload = self._build_context_resolver_payload(payload_json)
                return await self._run_subagent(context_resolver_agent, enriched_payload)

            @function_tool
            async def run_chart_manager_agent(payload_json: str) -> str:
                """Run ChartManager Agent with a JSON payload; returns JSON string."""
                output = await self._run_subagent(chart_manager_agent, payload_json)
                active_context = _get_active_context()
                records = (
                    active_context.aer_candidates
                    if active_context is not None
                    and isinstance(active_context.aer_candidates, list)
                    else []
                )
                refreshed = refresh_aer_store(
                    records,
                    output,
                    chart_manager_request=payload_json,
                    live_state=(
                        active_context.active_chart
                        if active_context is not None
                        else None
                    ),
                )
                return attach_aer_records(output, refreshed)

            @function_tool
            async def run_schema_explorer_agent(payload_json: str) -> str:
                """Run SchemaExplorer Agent with a JSON payload; returns JSON string."""
                return await self._run_subagent(schema_explorer_agent, payload_json)

            @function_tool
            async def run_answer_composer_agent(payload_json: str) -> str:
                """Run Answer Composer Agent with a JSON payload; returns JSON string."""
                active_context = _get_active_context()
                records = (
                    active_context.aer_candidates
                    if active_context is not None
                    and isinstance(active_context.aer_candidates, list)
                    else []
                )
                enriched_payload = build_answer_evidence_payload(
                    payload_json, records
                )
                return await self._run_subagent(
                    answer_composer_agent, enriched_payload
                )

            @function_tool
            async def run_docs_retriever_agent(payload_json: str) -> str:
                """Run DocsRetriever Agent with a JSON payload; returns JSON string."""
                return await self._run_subagent(docs_retriever_agent, payload_json)
        else:
            run_context_resolver_agent = None
            run_chart_manager_agent = None
            run_schema_explorer_agent = None
            run_answer_composer_agent = None
            run_docs_retriever_agent = None

        orchestrator_agent = Agent(
            name="Orchestrator Agent",
            model=selected_model,
            tools=[
                run_context_resolver_agent,
                run_chart_manager_agent,
                run_schema_explorer_agent,
                run_answer_composer_agent,
                run_docs_retriever_agent,
            ]
            if function_tool
            else [],
            model_settings=model_settings,
            instructions=prompts.ORCHESTRATOR_AGENT,
        )

        return {
            "orchestrator": orchestrator_agent,
            "context_resolver": context_resolver_agent,
            "chart_manager": chart_manager_agent,
            "schema_explorer": schema_explorer_agent,
            "answer_composer": answer_composer_agent,
            "docs_retriever": docs_retriever_agent,
            "insight_seeker": insight_seeker_agent,
        }

    @staticmethod
    def _build_context_resolver_payload(payload_json: str) -> str:
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError):
            payload = {"user_question": str(payload_json)}
        if not isinstance(payload, dict):
            payload = {"user_question": str(payload_json)}

        active_context = _get_active_context()
        if active_context is not None:
            payload["live_state"] = active_context.active_chart or {}
            payload["aer_candidates"] = active_context.aer_candidates or []
            payload["trace_logs"] = active_context.trace_logs or []
        else:
            payload.setdefault("live_state", {})
            payload.setdefault("aer_candidates", [])
            payload.setdefault("trace_logs", [])
        return json.dumps(payload, ensure_ascii=False, default=str)

    async def _run_subagent(self, agent: Any, payload_json: str) -> str:
        if Runner is None:
            raise RuntimeError(_IMPORT_ERROR or "agents Runner unavailable")
        prompt = [{"role": "user", "content": payload_json}]
        active_context = _get_active_context()
        result = await self._run_with_limits(
            agent,
            prompt,
            active_context,
            max_turns=max(1, self._max_turns - 2),
        )
        for attr in ("final_output", "output_text", "output"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)

    def _is_insights_command(self, message: str) -> bool:
        return message.strip().lower().startswith("/insights")

    async def _call_insights(
        self,
        history: list[dict[str, str]],
        context_obj: Any | None,
    ) -> str:
        payload = {
            "mode": "insights",
            "history": history[-50:],
            "trace": getattr(context_obj, "trace_logs", None) if context_obj else None,
            "active": getattr(context_obj, "active_chart", None) if context_obj else None,
        }
        return await self._run_subagent(
            self._insight_seeker_agent, json.dumps(payload, ensure_ascii=False)
        )

    async def respond(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
        context_obj: Any | None = None,
        debug: bool = False,
        model_name: str | None = None,
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        await self._ensure_model(model_name)
        if not self._orchestrator_agent:
            return (
                "Agent not available. "
                "Install the OpenAI agents package and set OPENAI_API_KEY."
            ), [{"type": "error", "message": "agent_not_available"}] if debug else [], None

        debug_items: list[dict[str, Any]] = []
        context_token = _ACTIVE_CONTEXT_VAR.set(context_obj)
        try:
            prompt = self._build_input_messages(
                message,
                history,
                context=self._inject_docs(context),
            )
            if context_obj is not None:
                context_obj.messages = prompt
            if self._is_insights_command(message) and self._insight_seeker_agent:
                insights_text = await self._call_insights(history, context_obj)
                insights_answer = self._parse_json_answer(insights_text)
                return insights_answer, [], insights_text
            result = await self._run_agent(self._orchestrator_agent, prompt, context_obj)
        except Exception as exc:
            if debug:
                debug_items.append({"type": "error", "message": str(exc)})
            return "Agent call failed. Check API credentials and logs.", debug_items, None
        finally:
            _ACTIVE_CONTEXT_VAR.reset(context_token)
        answer = self._extract_answer(result)
        raw_output = self._extract_raw_output(result)
        if debug:
            debug_items.extend(self._extract_debug_items(result))
        return answer, debug_items, raw_output

    async def respond_stream(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
        context_obj: Any | None = None,
        debug: bool = False,
        model_name: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        await self._ensure_model(model_name)
        if not self._orchestrator_agent:
            yield {"event": "error", "message": "agent_not_available"}
            return

        last_text = ""
        context_token = _ACTIVE_CONTEXT_VAR.set(context_obj)
        try:
            prompt = self._build_input_messages(
                message,
                history,
                context=self._inject_docs(context),
            )
            if context_obj is not None:
                context_obj.messages = prompt
            if self._is_insights_command(message) and self._insight_seeker_agent:
                insights_text = await self._call_insights(history, context_obj)
                insights_answer = self._parse_json_answer(insights_text)
                yield {"event": "final", "answer": insights_answer, "raw": insights_text}
                return
            stream = Runner.run_streamed(self._orchestrator_agent, prompt, context=context_obj)
            async for event in stream.stream_events():
                if RunItemStreamEvent and isinstance(event, RunItemStreamEvent):
                    name = event.name
                    item = event.item
                    if name == "reasoning_item_created":
                        yield {"event": "reasoning", "detail": "Reasoning ..."}
                    elif name == "tool_called":
                        tool_name = getattr(item.raw_item, "name", None)
                        tool_args = getattr(item.raw_item, "arguments", None)
                        yield {
                            "event": "tool_called",
                            "tool": tool_name,
                            "arguments": tool_args,
                        }
                    elif name == "tool_output":
                        output = getattr(item, "output", None)
                        yield {"event": "tool_output", "output": output}
                    elif name in ("message_output_created", "message_output_updated"):
                        raw_item = getattr(item, "raw_item", None)
                        text = None
                        if isinstance(raw_item, dict):
                            content = raw_item.get("content")
                            if isinstance(content, list) and content:
                                text = content[0].get("text")
                        if text is None:
                            text = getattr(item, "output_text", None)
                        if isinstance(text, str):
                            last_text = text
                            yield {"event": "message", "text": text}
        except Exception as exc:
            yield {"event": "error", "message": str(exc)}
            return
        finally:
            _ACTIVE_CONTEXT_VAR.reset(context_token)

        final_text = getattr(stream, "final_output", None) or last_text
        raw_text = final_text if isinstance(final_text, str) else str(final_text)
        answer = self._parse_json_answer(raw_text)
        yield {"event": "final", "answer": answer, "raw": raw_text}

    def _build_input_messages(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if context:
            messages.append({"role": "system", "content": context})
        for item in history[-20:]:
            role = (item.get("role") or "").strip()
            content = (item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({"role": role, "content": content})
        if not (
            history
            and (history[-1].get("role") or "").strip() == "user"
            and (history[-1].get("content") or "").strip() == message.strip()
        ):
            messages.append({"role": "user", "content": message})
        return messages

    def _load_dashboard_tools_doc(self) -> str | None:
        doc = _read_doc_file(Path("dashboard_tools.md"))
        if isinstance(doc, str):
            return doc
        return None

    def _inject_docs(self, context: str | None) -> str | None:
        if not self._dashboard_tools_doc:
            return context
        prefix = f"[DOC] dashboard_tools.md\n{self._dashboard_tools_doc}\n"
        if context:
            return prefix + context
        return prefix

    async def _run_agent(self, agent: Any, prompt: str, context_obj: Any | None) -> Any:
        if Runner is None:
            raise RuntimeError(_IMPORT_ERROR or "agents Runner unavailable")

        run_async = getattr(Runner, "run", None)
        if callable(run_async):
            return await self._run_with_limits(
                agent,
                prompt,
                context_obj,
                max_turns=self._max_turns,
            )

        run_sync = getattr(Runner, "run_sync", None)
        if callable(run_sync):
            return await asyncio.wait_for(
                asyncio.to_thread(
                    run_sync,
                    agent,
                    prompt,
                    context=context_obj,
                    max_turns=self._max_turns,
                ),
                timeout=self._timeout_sec,
            )

        raise RuntimeError("agents Runner has no run method")

    async def _run_with_limits(
        self,
        agent: Any,
        prompt: Any,
        context_obj: Any | None,
        *,
        max_turns: int,
    ) -> Any:
        run_async = getattr(Runner, "run", None)
        if not callable(run_async):
            raise RuntimeError("agents Runner has no async run method")
        result = run_async(
            agent,
            prompt,
            context=context_obj,
            max_turns=max_turns,
        )
        if asyncio.iscoroutine(result):
            return await asyncio.wait_for(result, timeout=self._timeout_sec)
        return result

    def _extract_answer(self, result: Any) -> str:
        if isinstance(result, str):
            return self._parse_json_answer(result)

        for attr in ("final_output", "output_text", "output"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return self._parse_json_answer(value)
        if isinstance(result, dict):
            return self._parse_json_answer(json.dumps(result))
        return str(result)

    def _parse_json_answer(self, text: str) -> str:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict):
            answer = payload.get("answer") or payload.get("message") or payload.get("text")
            if isinstance(answer, str) and answer.strip():
                return answer
        return text

    def _extract_raw_output(self, result: Any) -> str | None:
        if isinstance(result, str):
            return result
        for attr in ("final_output", "output_text", "output"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(result, dict):
            try:
                return json.dumps(result)
            except TypeError:
                return str(result)
        return str(result) if result is not None else None

    def _extract_debug_items(self, result: Any) -> list[dict[str, Any]]:
        items = []
        new_items = getattr(result, "new_items", None)
        if not isinstance(new_items, list):
            final_text = self._extract_answer(result)
            if final_text:
                items.append({"type": "final_output", "final_output": final_text})
            return items
        doc_tools = {
            "read_dashboard_tools_doc": "dashboard tools",
            "read_schema_explorer_doc": "schema explorer",
            "read_semantic_tools_doc": "semantic tools",
            "read_charts_doc": "charts",
        }
        for item in new_items:
            item_type = getattr(item, "type", None) or "unknown_item"
            if item_type == "reasoning_item":
                items.append({"type": item_type, "detail": "Reasoning ..."})
                continue
            raw = getattr(item, "raw_item", None)
            if item_type == "tool_call_item":
                name = getattr(raw, "name", None)
                if name is None and isinstance(raw, dict):
                    name = raw.get("name")
                if name in doc_tools:
                    items.append(
                        {
                            "type": item_type,
                            "name": f"reading {doc_tools[name]} document...",
                        }
                    )
                else:
                    items.append({"type": item_type, "name": name or "unknown_tool"})
                continue
            if item_type == "tool_call_output_item":
                output = None
                if isinstance(raw, dict):
                    output = raw.get("output")
                name = None
                if isinstance(raw, dict):
                    name = raw.get("name") or raw.get("tool_name")
                if name in doc_tools:
                    items.append({"type": item_type, "output": "documentation loaded"})
                else:
                    items.append({"type": item_type, "output": output})
                continue
            items.append({"type": item_type})
        final_text = self._extract_answer(result)
        if final_text:
            items.append({"type": "final_output", "final_output": final_text})
        return items
