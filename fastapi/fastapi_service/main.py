from __future__ import annotations
import duckdb
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal
from pathlib import Path
from dataclasses import replace
import math
import os

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import OpenAI

from fastapi_service import db
from fastapi_service.config import Settings, load_settings
from fastapi_service.agent import AgentRunner, AgentContext
from fastapi_service.context_manager import AERStore
from fastapi_service.models import (
    ChatRequest,
    ChatResponse,
    DashboardOnlyRequest,
    DashboardOnlyResponse,
    EventRequest,
    StatusResponse,
    ViewSpec,
    CreateViewResult,
    SupersetDatasetSyncRequest,
    SupersetDatasetSyncResult,
    ChartCreateRequest,
    ChartCreateResponse,
    DashboardAppendChartRequest,
    DashboardAppendChartResponse,
)
from fastapi_service.superset import (
    QueryTranslater,
    SupersetPoller,
    append_chart_to_dashboard_layout,
    fetch_dashboard_charts,
    fetch_dashboard_layout,
    fetch_dashboard_default_tab,
    fetch_dataset_schema,
    fetch_dataset_data,
    lookup_user_id,
    fetch_chart_data_from_log,
    fetch_chart_form_data,
    fetch_chart_queries,
    normalize_chart_queries_result,
    lookup_username,
)
from fastapi_service.superset_client import (
    _api_session_with_bearer,
    _ensure_csrf,
    _get_base_url,
)
from fastapi_service.semantic import fetch_cube_meta
from fastapi_service.cube_conf import load_repo_schema
from fastapi_service.semantic import (
    create_cube_view,
    delete_cube_view,
    sync_superset_dataset,
    log_create_view,
    log_dataset_sync,
    log_unified_event,
)
from fastapi_service.writer import DuckDBWriter

logger = logging.getLogger(__name__)
_CHART_TEMPLATES_PATH = Path(__file__).resolve().parents[1] / "chart_templates.json"


class UserDuckDBStore:
    def __init__(self, base_path: str) -> None:
        self._base_path = base_path
        self._entries: dict[str, tuple[duckdb.DuckDBPyConnection, DuckDBWriter]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self, user_key: str | None
    ) -> tuple[duckdb.DuckDBPyConnection, DuckDBWriter, str]:
        path = db.resolve_user_duckdb_path(self._base_path, user_key)
        existing = self._entries.get(path)
        if existing:
            conn, writer = existing
            return conn, writer, path

        async with self._lock:
            existing = self._entries.get(path)
            if existing:
                conn, writer = existing
                return conn, writer, path
            conn = db.connect(path)
            db.init_schema(conn)
            writer = DuckDBWriter(conn)
            await writer.start()
            self._entries[path] = (conn, writer)
            return conn, writer, path

    async def stop_all(self) -> None:
        for _path, (conn, writer) in list(self._entries.items()):
            try:
                await writer.stop()
            finally:
                conn.close()
        self._entries.clear()

    def get_existing(
        self, user_key: str | None
    ) -> tuple[duckdb.DuckDBPyConnection, DuckDBWriter, str] | None:
        path = db.resolve_user_duckdb_path(self._base_path, user_key)
        existing = self._entries.get(path)
        if not existing:
            return None
        conn, writer = existing
        return conn, writer, path


def _load_chart_templates() -> dict[str, Any]:
    candidates = [
        _CHART_TEMPLATES_PATH,
        Path.cwd() / "fastapi" / "chart_templates.json",
        Path.cwd() / "chart_templates.json",
        Path(__file__).resolve().parents[2] / "chart_templates.json",
        Path(__file__).resolve().parents[2] / "fastapi" / "chart_templates.json",
    ]
    path = None
    for candidate in candidates:
        if candidate.exists():
            path = candidate
            break
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _summarize_chart_data(chart_data: Any | None) -> dict[str, Any]:
    if isinstance(chart_data, list):
        primary = chart_data[0] if chart_data else []
        summary = chart_data[1] if len(chart_data) > 1 else []
        if isinstance(primary, list):
            out = {"rows": primary[:10], "row_count": len(primary)}
            if isinstance(summary, list) and summary:
                out["summary_rows"] = summary[:10]
            return out
        if isinstance(primary, dict):
            return {"result": primary}
        return {"raw": chart_data}

    if not isinstance(chart_data, dict):
        return {}
    payload = chart_data.get("data") if isinstance(chart_data.get("data"), dict) else chart_data
    results = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(results, list) and results:
        result0 = results[0] if isinstance(results[0], dict) else {}
        data = result0.get("data")
        if isinstance(data, list):
            return {"rows": data[:10], "row_count": len(data)}
        return {"result": result0}
    return {"raw": payload}


def _fetch_latest_chart_log(
    conn: duckdb.DuckDBPyConnection,
    chart_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT superset_log_id, dttm, action, dashboard_id, slice_id, json
        FROM superset_action_logs
        WHERE slice_id = ? AND action = 'ChartDataRestApi.data'
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        [chart_id],
    ).fetchone()
    if not row:
        return None
    payload_json = row[5] or ""
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError:
        payload = {}
    return {
        "superset_log_id": row[0],
        "dttm": row[1].isoformat() if row[1] else None,
        "action": row[2],
        "dashboard_id": row[3],
        "slice_id": row[4],
        "payload": payload,
    }


def _normalize_legend_interaction(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    selected = payload.get("selected")
    if isinstance(selected, dict) and selected:
        # If everything is selected, treat as no interaction.
        if (
            all(bool(value) for value in selected.values())
            and payload.get("legend_name") is None
            and payload.get("clicked_name") is None
            and payload.get("legend_active") is None
        ):
            return None
    return payload


def _fetch_latest_active_chart(
    conn: duckdb.DuckDBPyConnection,
    dashboard_id: int | None = None,
) -> dict[str, Any] | None:
    def has_active_filters(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        form_data = payload.get("form_data") or payload.get("from_data") or {}
        if not isinstance(form_data, dict):
            return False
        filters = form_data.get("filters")
        if not isinstance(filters, list) or not filters:
            return False
        for entry in filters:
            if not isinstance(entry, dict):
                continue
            val = entry.get("val")
            if isinstance(val, str):
                if val.strip() and val.strip().lower() != "no filter":
                    return True
                continue
            if isinstance(val, list):
                if any(
                    isinstance(item, str) and item.strip() and item.strip().lower() != "no filter"
                    for item in val
                ):
                    return True
                continue
            if val not in (None, "", []):
                return True
        return False

    latest_filters = []
    latest_params: list[Any] = []
    if dashboard_id is not None:
        latest_filters.append("dashboard_id = ?")
        latest_params.append(dashboard_id)
    latest_where = f"WHERE {' AND '.join(latest_filters)}" if latest_filters else ""
    latest_row = conn.execute(
        f"""
        SELECT superset_log_id, dttm, action, dashboard_id, slice_id, json
        FROM superset_action_logs
        {latest_where}
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        latest_params,
    ).fetchone()
    if latest_row:
        latest_action = latest_row[2]
        if latest_action == "DashboardRestApi.get":
            return None
        if latest_action == "ChartDataRestApi.data":
            payload_json = latest_row[5] or ""
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except json.JSONDecodeError:
                payload = {}
            if not has_active_filters(payload):
                return None
            return {
                "superset_log_id": latest_row[0],
                "dttm": latest_row[1].isoformat() if latest_row[1] else None,
                "action": latest_row[2],
                "dashboard_id": latest_row[3],
                "slice_id": latest_row[4],
            }

    filters = ["slice_id IS NOT NULL"]
    params: list[Any] = []
    if dashboard_id is not None:
        filters.append("dashboard_id = ?")
        params.append(dashboard_id)
    where_clause = " AND ".join(filters)
    row = conn.execute(
        f"""
        SELECT superset_log_id, dttm, action, dashboard_id, slice_id
        FROM superset_action_logs
        WHERE {where_clause}
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    return {
        "superset_log_id": row[0],
        "dttm": row[1].isoformat() if row[1] else None,
        "action": row[2],
        "dashboard_id": row[3],
        "slice_id": row[4],
    }


def _fetch_latest_chart_activity(
    conn: duckdb.DuckDBPyConnection,
    dashboard_id: int | None = None,
) -> dict[str, Any] | None:
    filters = ["action = 'ChartDataRestApi.data'"]
    params: list[Any] = []
    if dashboard_id is not None:
        filters.append("dashboard_id = ?")
        params.append(dashboard_id)
    where_clause = " AND ".join(filters)
    row = conn.execute(
        f"""
        SELECT superset_log_id, dttm, action, dashboard_id, slice_id
        FROM superset_action_logs
        WHERE {where_clause}
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    return {
        "superset_log_id": row[0],
        "dttm": row[1].isoformat() if row[1] else None,
        "action": row[2],
        "dashboard_id": row[3],
        "slice_id": row[4],
    }


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
        "native_filter_added",
        "native_filter_removed",
        "global_filter_added",
        "global_filter_removed",
        # backward compatibility with old UI event names
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
    where_clause = " AND ".join(filters)
    rows = conn.execute(
        f"""
        SELECT superset_log_id, dttm, action, user_id, dashboard_id, slice_id, json
        FROM superset_action_logs
        WHERE {where_clause}
        ORDER BY superset_log_id DESC
        LIMIT 300
        """,
        params,
    ).fetchall()
    if not rows:
        return None
    for row in rows:
        payload = None
        raw_json = row[6]
        if isinstance(raw_json, str) and raw_json:
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                payload = None
        if session_id:
            row_session_id = (
                payload.get("session_id") if isinstance(payload, dict) else None
            )
            if str(row_session_id or "") != str(session_id):
                continue
        return {
            "superset_log_id": row[0],
            "dttm": row[1].isoformat() if row[1] else None,
            "action": row[2],
            "user_id": row[3],
            "dashboard_id": row[4],
            "slice_id": row[5],
            "payload": payload.get("payload") if isinstance(payload, dict) else None,
        }
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
    # Reject value-label artifacts like "2023-07-01, 2022-07-01".
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
        payload = {}
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
    if session_id:
        where.append("json_extract_string(json, '$.session_id') = ?")
        params.append(session_id)
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


def _fetch_latest_ui_session_id(
    conn: duckdb.DuckDBPyConnection,
    dashboard_id: int | None = None,
) -> str | None:
    filters = [
        "json_extract_string(json, '$.source') = 'ui'",
        "json_extract_string(json, '$.session_id') IS NOT NULL",
        "json_extract_string(json, '$.session_id') <> ''",
    ]
    params: list[Any] = []
    if dashboard_id is not None:
        filters.append("dashboard_id = ?")
        params.append(dashboard_id)
    where_clause = " AND ".join(filters)
    row = conn.execute(
        f"""
        SELECT json_extract_string(json, '$.session_id') AS session_id
        FROM superset_action_logs
        WHERE {where_clause}
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    value = row[0]
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _summarize_chart_log(log: dict[str, Any] | None) -> dict[str, Any]:
    if not log or not isinstance(log, dict):
        return {}
    payload = log.get("payload") or {}
    form_data = payload.get("form_data") if isinstance(payload, dict) else None
    queries = payload.get("queries") if isinstance(payload, dict) else None
    datasource = payload.get("datasource") if isinstance(payload, dict) else None
    result = None
    if isinstance(payload, dict) and (
        payload.get("data") is not None or payload.get("result") is not None
    ):
        result = _summarize_chart_data(payload)
    return {
        "superset_log_id": log.get("superset_log_id"),
        "slice_id": log.get("slice_id"),
        "dashboard_id": log.get("dashboard_id"),
        "action": log.get("action"),
        "observed_at": log.get("dttm"),
        "form_data": form_data,
        "queries": queries,
        "datasource": datasource,
        "result": result,
    }


def _build_active_charts_prompt_context(
    conn: duckdb.DuckDBPyConnection,
    active_context: dict[str, Any] | None,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    if not isinstance(active_context, dict):
        return []
    active_charts = active_context.get("active_charts")
    if not isinstance(active_charts, list) or not active_charts:
        return []
    out: list[dict[str, Any]] = []
    for chart in active_charts[: max(1, int(limit))]:
        if not isinstance(chart, dict):
            continue
        slice_id = chart.get("slice_id")
        try:
            sid = int(slice_id) if slice_id is not None else None
        except Exception:
            sid = None
        log = _fetch_latest_chart_log(conn, sid) if sid is not None else None
        out.append(
            {
                "slice_id": sid,
                "name": chart.get("name"),
                "tab": chart.get("tab"),
                "interaction": chart.get("interaction"),
                "native_filters": chart.get("native_filters"),
                "cross_filters": chart.get("cross_filters"),
                "chart_log": _summarize_chart_log(log) if log else None,
            }
        )
    return out


def _estimate_token_usage(message: str, response: str) -> dict[str, int]:
    # Lightweight fallback estimate when model usage metadata is unavailable.
    prompt_tokens = int(math.ceil(len(message or "") / 4)) if message else 0
    completion_tokens = int(math.ceil(len(response or "") / 4)) if response else 0
    total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _estimate_usd_cost(prompt_tokens: int, completion_tokens: int) -> float:
    input_per_1k = float(os.getenv("AGENT_INPUT_COST_PER_1K_USD", "0"))
    output_per_1k = float(os.getenv("AGENT_OUTPUT_COST_PER_1K_USD", "0"))
    return round((prompt_tokens / 1000.0) * input_per_1k + (completion_tokens / 1000.0) * output_per_1k, 8)


def _load_dialogue_history(
    conn: duckdb.DuckDBPyConnection,
    session_id: str,
    limit: int,
) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT ts, message, response
        FROM streamlit_chat_logs
        WHERE session_id = ?
        ORDER BY ts DESC
        LIMIT ?
        """,
        [session_id, limit],
    ).fetchall()
    history: list[dict[str, str]] = []
    for _ts, message, response in reversed(rows):
        if message:
            history.append({"role": "user", "content": message})
        if response:
            history.append({"role": "assistant", "content": response})
    return history


def _normalize_request_history(
    history: list[dict[str, Any]] | None,
    limit: int = 20,
) -> list[dict[str, str]]:
    if not isinstance(history, list):
        return []
    out: list[dict[str, str]] = []
    for item in history[-max(1, int(limit)):]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content:
            continue
        out.append({"role": role, "content": content})
    return out


def _load_recent_trace_logs(
    conn: duckdb.DuckDBPyConnection,
    session_id: str | None,
    dashboard_id: int | None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    normalized_session_id = (
        session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
    )
    filters: list[str] = []
    params: list[Any] = []
    if normalized_session_id:
        filters.append("json_extract_string(json, '$.session_id') = ?")
        params.append(normalized_session_id)
    elif dashboard_id is not None:
        filters.append("dashboard_id = ?")
        params.append(dashboard_id)
    else:
        return []

    where_clause = " AND ".join(filters)
    rows = conn.execute(
        f"""
        SELECT superset_log_id, dttm, action, user_id, dashboard_id, slice_id, json
        FROM superset_action_logs
        WHERE {where_clause}
        ORDER BY superset_log_id DESC
        LIMIT ?
        """,
        [*params, max(1, int(limit))],
    ).fetchall()

    out: list[dict[str, Any]] = []
    for superset_log_id, dttm, action, user_id, row_dashboard_id, slice_id, raw_json in reversed(rows):
        payload_obj: dict[str, Any] = {}
        if isinstance(raw_json, str) and raw_json:
            try:
                payload_obj = json.loads(raw_json)
            except json.JSONDecodeError:
                payload_obj = {}
        payload = payload_obj.get("payload") if isinstance(payload_obj, dict) else None
        out.append(
            {
                "superset_log_id": superset_log_id,
                "dttm": dttm.isoformat() if isinstance(dttm, datetime) else str(dttm),
                "action": action,
                "user_id": user_id,
                "dashboard_id": row_dashboard_id,
                "slice_id": slice_id,
                "payload": payload if isinstance(payload, dict) else None,
            }
        )
    return out


def _decorate_superset_log(row: dict[str, Any]) -> dict[str, Any]:
    original_action = row.get("action")
    original_user_id = row.get("user_id")
    raw_json = row.get("json")
    payload: dict[str, Any] | None = None
    if isinstance(raw_json, str):
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            payload = None
    elif isinstance(raw_json, dict):
        payload = raw_json
    if payload and isinstance(payload, dict):
        resolved_raw = payload.get("resolved_user_key")
        resolved_user_key = (
            resolved_raw.strip()
            if isinstance(resolved_raw, str) and resolved_raw.strip()
            else None
        )
        if resolved_user_key:
            row["user_key"] = resolved_user_key
            row["superset_user_id"] = original_user_id
            row["user_id"] = resolved_user_key
        if payload.get("payload") is not None and "payload" not in row:
            row["payload"] = payload.get("payload")
        if row.get("user_id") is None and payload.get("user_id") is not None:
            row["user_id"] = payload.get("user_id")
        event_name = payload.get("event_name")
        if event_name:
            row["event_name"] = event_name
            if original_action == "log":
                row["action_raw"] = original_action
                row["action"] = event_name
                row["action_label"] = event_name
            else:
                row["action_label"] = f"log:{event_name}"
        if event_name == "further_drill_by":
            row["action_label"] = "drill_by"
            row["drill_by"] = {
                "slice_id": payload.get("slice_id"),
                "drill_depth": payload.get("drill_depth"),
                "drill_column": payload.get("drill_column"),
                "drill_column_label": payload.get("drill_column_label"),
                "drill_groupby_field": payload.get("drill_groupby_field"),
                "drill_adhoc_filter_field": payload.get(
                    "drill_adhoc_filter_field"
                ),
                "drill_filters": payload.get("drill_filters"),
            }
        if payload.get("path") == "/datasource/samples":
            row["event_name"] = row.get("event_name") or "drill_to_details"
            row["action_label"] = "drill_to_details"
            row["sample_filters"] = payload.get("filters")
        if not row.get("slice_id") and payload.get("slice_id"):
            row["slice_id"] = payload.get("slice_id")
    return row


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    db_store = UserDuckDBStore(settings.duckdb_path)
    # Keep a global log store and fan-out Superset logs into user-scoped stores.
    conn, writer, _ = await db_store.get_or_create(None)
    app.state.db_store = db_store
    app.state.conn = conn
    app.state.writer = writer
    app.state.superset_poller = None
    app.state.superset_task = None

    if settings.superset_meta_db_uri:
        user_id_to_username: dict[int, str | None] = {}

        async def _resolve_superset_user_key(row: dict[str, Any]) -> str | None:
            raw_user_id = row.get("user_id")
            if raw_user_id is None:
                return None
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                return None
            if user_id in user_id_to_username:
                return user_id_to_username[user_id]
            resolved = await asyncio.to_thread(
                lookup_username, settings.superset_meta_db_uri, user_id
            )
            normalized = resolved.strip() if isinstance(resolved, str) else ""
            user_key = normalized or None
            user_id_to_username[user_id] = user_key
            return user_key

        async def _ingest_superset_log(
            payload: dict[str, Any],
            row: dict[str, Any],
        ) -> None:
            user_key = await _resolve_superset_user_key(row)
            base_payload = dict(payload)
            raw_json = base_payload.get("json")
            if isinstance(raw_json, str) and raw_json and user_key:
                try:
                    parsed = json.loads(raw_json)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    parsed.setdefault("resolved_user_key", user_key)
                    if "superset_user_id" not in parsed and parsed.get("user_id") is not None:
                        parsed["superset_user_id"] = parsed.get("user_id")
                    parsed["user_id"] = user_key
                    base_payload["json"] = json.dumps(parsed, ensure_ascii=False)
            await writer.enqueue_superset_log(base_payload)
            if not user_key:
                return
            _user_conn, user_writer, _user_path = await db_store.get_or_create(user_key)
            user_payload = dict(base_payload)
            await user_writer.enqueue_superset_log(user_payload)

        poller = SupersetPoller(
            meta_db_uri=settings.superset_meta_db_uri,
            poll_interval_sec=settings.superset_poll_interval_sec,
            batch_size=settings.superset_batch_size,
            dashboard_id=settings.superset_log_dashboard_id,
            user_id=settings.superset_log_user_id,
            # Do not pin poller ingest to a fixed username; ingest all users.
            username=None,
            writer=writer,
            conn=conn,
            ingest_superset_log=_ingest_superset_log,
        )
        app.state.superset_poller = poller
        app.state.superset_task = asyncio.create_task(poller.run())
        logger.info("Superset poller started")
    else:
        logger.info("Superset poller disabled (SUPERSET_META_DB_URI not set)")

    try:
        yield
    finally:
        superset_poller = app.state.superset_poller
        if superset_poller:
            await superset_poller.stop()
        superset_task = app.state.superset_task
        if superset_task:
            await superset_task
        db_store = app.state.db_store
        await db_store.stop_all()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="TwinBI🐝 FastAPI", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.agent_runner = AgentRunner()
    app.state.aer_store = AERStore()
    app.state.last_chat_context = None
    app.state.last_chat_debug = None
    app.state.context_cleared = False
    app.state.session_meta: dict[str, dict[str, Any]] = {}

    def _resolve_superset_settings(
        request: Request | None = None,
        payload: ChatRequest | SupersetDatasetSyncRequest | None = None,
    ) -> Settings:
        base_settings = app.state.settings
        header_username = None
        header_password = None
        if request is not None:
            header_username = request.headers.get("X-Superset-Username")
            header_password = request.headers.get("X-Superset-Password")
        payload_username = payload.superset_username if payload is not None else None
        payload_password = payload.superset_password if payload is not None else None

        username = (
            (payload_username.strip() if isinstance(payload_username, str) else None)
            or (header_username.strip() if isinstance(header_username, str) else None)
        )
        password = (
            payload_password
            if isinstance(payload_password, str) and payload_password != ""
            else (
                header_password
                if isinstance(header_password, str) and header_password != ""
                else None
            )
        )
        if username and password:
            return replace(
                base_settings,
                superset_username=username,
                superset_password=password,
            )
        return base_settings

    @app.get("/health", response_model=StatusResponse)
    def health() -> StatusResponse:
        return StatusResponse(status="ok")

    @app.get("/poller/status")
    def poller_status() -> dict[str, Any]:
        poller = app.state.superset_poller
        if not poller:
            return {"enabled": False, "reason": "SUPERSET_META_DB_URI not set"}
        return {"enabled": True, **poller.status()}

    @app.post("/poller/reset", response_model=StatusResponse)
    def poller_reset() -> StatusResponse:
        conn = app.state.conn
        db.delete_checkpoint(conn, db.CHECKPOINT_KEY_SUPERSET_LAST_ID)
        return StatusResponse(status="ok")

    @app.post("/poller/trigger", response_model=StatusResponse)
    async def poller_trigger() -> StatusResponse:
        poller = app.state.superset_poller
        if not poller:
            return StatusResponse(status="disabled")
        await poller.poll_once()
        return StatusResponse(status="ok")

    @app.post("/poller/sync")
    async def poller_sync() -> dict[str, Any]:
        poller = app.state.superset_poller
        if not poller:
            return {"status": "disabled", "inserted": {"logs": 0, "chat": 0}}
        inserted = await poller.sync_missing()
        return {"status": "ok", "inserted": {"logs": inserted, "chat": 0}}

    def _normalize_user_key(user_key: Any) -> str | None:
        if not isinstance(user_key, str):
            return None
        text = user_key.strip()
        return text or None

    def _normalize_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
                try:
                    return int(text)
                except ValueError:
                    return None
        return None

    def _extract_superset_identity(payload: dict[str, Any]) -> tuple[str | None, int | None]:
        me_raw = payload.get("me")
        me_payload = me_raw if isinstance(me_raw, dict) else {}
        me_result = (
            me_payload.get("result")
            if isinstance(me_payload.get("result"), dict)
            else {}
        )
        username = _normalize_user_key(
            payload.get("username")
            or payload.get("superset_username")
            or me_result.get("username")
            or me_payload.get("username")
        )
        superset_user_id = _normalize_int(
            payload.get("superset_user_id")
            or payload.get("user_id")
            or me_result.get("user_id")
            or me_result.get("id")
            or me_payload.get("user_id")
            or me_payload.get("id")
        )
        return username, superset_user_id

    def _extract_dashboard_id(payload: dict[str, Any] | None) -> int | None:
        if not isinstance(payload, dict):
            return None
        for key in ("dashboard_id", "dashboardId"):
            parsed = _normalize_int(payload.get(key))
            if parsed is not None:
                return parsed
        inner = payload.get("payload")
        if isinstance(inner, dict):
            for key in ("dashboard_id", "dashboardId"):
                parsed = _normalize_int(inner.get(key))
                if parsed is not None:
                    return parsed
        referrer = payload.get("referrer")
        if isinstance(referrer, str):
            match = re.search(r"/dashboard/(\d+)", referrer)
            if match:
                return _normalize_int(match.group(1))
        return None

    def _upsert_session_meta(
        *,
        session_id: str | None,
        username: str | None = None,
        superset_user_id: int | None = None,
        dashboard_id: int | None = None,
    ) -> None:
        sid = _normalize_user_key(session_id)
        if not sid:
            return
        existing = app.state.session_meta.get(sid) or {}
        if username:
            existing["username"] = username
        if superset_user_id is not None:
            existing["superset_user_id"] = superset_user_id
        if dashboard_id is not None:
            existing["dashboard_id"] = dashboard_id
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        app.state.session_meta[sid] = existing

    def _resolve_session_user_key(session_id: str | None) -> str | None:
        sid = _normalize_user_key(session_id)
        if not sid:
            return None
        meta = app.state.session_meta.get(sid)
        if not isinstance(meta, dict):
            return None
        return _normalize_user_key(meta.get("username"))

    def _resolve_session_dashboard_id(session_id: str | None) -> int | None:
        sid = _normalize_user_key(session_id)
        if not sid:
            return None
        meta = app.state.session_meta.get(sid)
        if not isinstance(meta, dict):
            return None
        return _normalize_int(meta.get("dashboard_id"))

    def _resolve_dashboard_id_from_user_logs(
        *,
        user_key: str | None,
        session_id: str | None,
    ) -> int | None:
        sid = _normalize_user_key(session_id)
        resolved_user = _normalize_user_key(user_key)
        if not sid or not resolved_user:
            return None
        existing = app.state.db_store.get_existing(resolved_user)
        if not existing:
            return None
        conn, _writer, _path = existing
        try:
            row = conn.execute(
                """
                SELECT dashboard_id, json_extract_string(json, '$.payload.dashboard_id')
                FROM superset_action_logs
                WHERE json_extract_string(json, '$.session_id') = ?
                  AND (
                    dashboard_id IS NOT NULL
                    OR json_extract_string(json, '$.payload.dashboard_id') IS NOT NULL
                  )
                ORDER BY superset_log_id DESC
                LIMIT 1
                """,
                [sid],
            ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        return _normalize_int(row[0]) or _normalize_int(row[1])

    def _resolve_effective_user_key(
        user_key: str | None = None,
        session_id: str | None = None,
    ) -> str | None:
        return _normalize_user_key(user_key) or _resolve_session_user_key(session_id)

    def _connect_logs_for_read(
        user_key: str | None = None,
        session_id: str | None = None,
    ) -> duckdb.DuckDBPyConnection:
        base_path = app.state.settings.duckdb_path
        resolved_user_key = _resolve_effective_user_key(user_key, session_id)
        if resolved_user_key:
            user_path = db.resolve_user_duckdb_path(base_path, resolved_user_key)
            if Path(user_path).exists():
                return db.connect(user_path, read_only=False)
        return db.connect(base_path, read_only=False)

    @app.get("/superset/logs/latest")
    def superset_logs_latest(
        dashboard_id: int | None = None,
        user_id: int | None = None,
        user_key: str | None = None,
        session_id: str | None = None,
        action: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        effective_user_key = _resolve_effective_user_key(user_key, session_id)
        conn = _connect_logs_for_read(user_key, session_id)
        try:
            filters = []
            params: list[Any] = []
            if dashboard_id is not None:
                filters.append("dashboard_id = ?")
                params.append(dashboard_id)
            if user_id is not None:
                filters.append("user_id = ?")
                params.append(user_id)
            if (
                effective_user_key is None
                and isinstance(session_id, str)
                and session_id.strip()
            ):
                filters.append("json_extract_string(json, '$.session_id') = ?")
                params.append(session_id.strip())
            if action is not None:
                filters.append("action = ?")
                params.append(action)
            where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
            sql = (
                "SELECT superset_log_id, dttm, action, user_id, dashboard_id, "
                "slice_id, duration_ms, referrer, json, ingested_at "
                "FROM superset_action_logs "
                f"{where_clause} "
                "ORDER BY dttm DESC NULLS LAST, ingested_at DESC "
                "LIMIT ?"
            )
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        rows_out = [
            {
                "superset_log_id": row[0],
                "dttm": row[1],
                "action": row[2],
                "user_id": row[3],
                "dashboard_id": row[4],
                "slice_id": row[5],
                "duration_ms": row[6],
                "referrer": row[7],
                "json": row[8],
                "ingested_at": row[9],
                "source": "superset",
            }
            for row in rows
        ]
        return [_decorate_superset_log(row) for row in rows_out]

    def _fetch_superset_logs_since(
        *,
        last_id: int,
        dashboard_id: int | None,
        user_id: int | None,
        user_key: str | None,
        session_id: str | None,
        action: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        effective_user_key = _resolve_effective_user_key(user_key, session_id)
        conn = _connect_logs_for_read(user_key, session_id)
        try:
            filters = ["superset_log_id > ?"]
            params: list[Any] = [last_id]
            if dashboard_id is not None:
                filters.append("dashboard_id = ?")
                params.append(dashboard_id)
            if user_id is not None:
                filters.append("user_id = ?")
                params.append(user_id)
            if (
                effective_user_key is None
                and isinstance(session_id, str)
                and session_id.strip()
            ):
                filters.append("json_extract_string(json, '$.session_id') = ?")
                params.append(session_id.strip())
            if action is not None:
                filters.append("action = ?")
                params.append(action)
            where_clause = " AND ".join(filters)
            sql = (
                "SELECT superset_log_id, dttm, action, user_id, dashboard_id, "
                "slice_id, duration_ms, referrer, json, ingested_at "
                "FROM superset_action_logs "
                f"WHERE {where_clause} "
                "ORDER BY superset_log_id DESC "
                "LIMIT ?"
            )
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        rows_out = [
            {
                "superset_log_id": row[0],
                "dttm": row[1],
                "action": row[2],
                "user_id": row[3],
                "dashboard_id": row[4],
                "slice_id": row[5],
                "duration_ms": row[6],
                "referrer": row[7],
                "json": row[8],
                "ingested_at": row[9],
            }
            for row in rows
        ]
        return [_decorate_superset_log(row) for row in rows_out]

    def _row_timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return datetime.min
        return datetime.min

    @app.get("/events/stream")
    async def events_stream(
        request: Request,
        source: str | None = None,
        dashboard_id: int | None = None,
        user_id: int | None = None,
        user_key: str | None = None,
        session_id: str | None = None,
        action: str | None = None,
        last_id: int = 0,
        limit: int = 100,
        poll_interval_sec: float = 1.0,
    ) -> StreamingResponse:
        translator = QueryTranslater(settings=app.state.settings)
        header_last_id = request.headers.get("Last-Event-ID") or request.headers.get(
            "last-event-id"
        )
        if header_last_id and header_last_id.isdigit():
            last_id = max(last_id, int(header_last_id))
        if source:
            source = source.lower()
        if source in ("ui", "all"):
            source = "superset"

        async def event_generator() -> Any:
            current_superset_id = last_id
            while True:
                if await request.is_disconnected():
                    break
                rows_out: list[dict[str, Any]] = []
                if source in (None, "", "superset"):
                    try:
                        rows = await asyncio.to_thread(
                            _fetch_superset_logs_since,
                            last_id=current_superset_id,
                            dashboard_id=dashboard_id,
                            user_id=user_id,
                            user_key=user_key,
                            session_id=session_id,
                            action=action,
                            limit=limit,
                        )
                    except Exception as exc:
                        logger.exception("Superset stream query failed: %s", exc)
                        yield ": error\n\n"
                        await asyncio.sleep(poll_interval_sec)
                        continue
                    for row in rows:
                        row_id = int(row["superset_log_id"] or 0)
                        current_superset_id = max(current_superset_id, row_id)
                        if (
                            row.get("action") in QueryTranslater.SUPPORTED_ACTIONS
                            and row.get("json")
                        ):
                            try:
                                parsed = translator.parse_payload(row["json"])
                            except Exception as exc:
                                logger.exception(
                                    "Superset SQL translate failed: %s", exc
                                )
                                parsed = None
                            if parsed:
                                row["translated_sql"] = parsed.get("sql")
                                row["translated_filters"] = parsed.get("filters")
                                row["translated_where"] = parsed.get("where")
                        row["source"] = "superset"
                        rows_out.append(row)
                if rows_out:
                    rows_out.sort(
                        key=lambda item: _row_timestamp(
                            item.get("dttm") or item.get("ts")
                        )
                    )
                    for row in rows_out:
                        payload = json.dumps(row, default=str)
                        yield f"id: {current_superset_id}\n" f"data: {payload}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(poll_interval_sec)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/superset/logs/latest_sql")
    def superset_logs_latest_sql(
        dashboard_id: int | None = None,
        slice_id: int | None = None,
    ) -> dict[str, Any]:
        conn = db.connect(app.state.settings.duckdb_path, read_only=False)
        try:
            filters = [
                "action = 'ChartDataRestApi.data'"
            ]
            params: list[Any] = []
            if dashboard_id is not None:
                filters.append("dashboard_id = ?")
                params.append(dashboard_id)
            if slice_id is not None:
                filters.append("slice_id = ?")
                params.append(slice_id)
            where_clause = " AND ".join(filters)
            sql = (
                "SELECT superset_log_id, slice_id, json "
                "FROM superset_action_logs "
                f"WHERE {where_clause} "
                "ORDER BY superset_log_id DESC "
                "LIMIT 1"
            )
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="no chart data logs found")
        translator = QueryTranslater(settings=app.state.settings)
        translated_sql = translator.translate_payload(row[2])
        return {
            "superset_log_id": row[0],
            "slice_id": row[1],
            "sql": translated_sql,
        }

    @app.get("/superset/datasets/{dataset_id}/schema")
    def superset_dataset_schema(dataset_id: int, request: Request) -> dict[str, Any]:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        try:
            schema = fetch_dataset_schema(settings, dataset_id)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc
        return schema

    @app.get("/superset/datasets")
    def superset_datasets(
        request: Request,
        user_id: int | None = None,
        username: str | None = None,
        name: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        if username and user_id:
            raise HTTPException(
                status_code=400,
                detail="Provide either user_id or username, not both",
            )

        if username and not user_id:
            meta_db_uri = settings.superset_meta_db_uri
            if not meta_db_uri:
                raise HTTPException(
                    status_code=400,
                    detail="Superset meta DB not configured",
                )
            try:
                user_id = lookup_user_id(meta_db_uri, username)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Superset API error: {exc}",
                ) from exc
            if not user_id:
                return {"count": 0, "result": []}

        try:
            session = _api_session_with_bearer(settings)
            base_url = _get_base_url(settings)
            _ensure_csrf(session, base_url)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

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
                raise HTTPException(
                    status_code=502,
                    detail=f"Superset API error: {response.text}",
                )
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

    @app.get("/superset/charts/templates")
    def superset_chart_templates(
        viz_type: str | None = None,
    ) -> dict[str, Any]:
        templates = _load_chart_templates()
        if not templates:
            return {"templates": []}
        if viz_type:
            entry = templates.get(viz_type)
            if entry is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"viz_type not found: {viz_type}",
                )
            return {"templates": {viz_type: entry}}
        return {"templates": templates}

    @app.post("/superset/charts", response_model=ChartCreateResponse)
    def superset_chart_create(
        payload: ChartCreateRequest,
        request: Request,
    ) -> ChartCreateResponse:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )

        templates = _load_chart_templates()
        template = templates.get(payload.viz_type)
        if not template:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported viz_type: {payload.viz_type}",
            )

        actual_viz_type = template.get("viz_type") or payload.viz_type
        form_data = dict(template.get("form_data") or {})
        form_data["viz_type"] = actual_viz_type
        form_data["datasource"] = f"{payload.dataset_id}__{payload.datasource_type}"

        enc = payload.encodings or {}
        opts = payload.options or {}
        warnings: list[str] = []

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

        # Common mappings
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

        # Pie expects "metric" and "groupby"
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
                raise HTTPException(
                    status_code=400,
                    detail="pie requires metric or metrics",
                )
            if not form_data.get("groupby"):
                raise HTTPException(
                    status_code=400,
                    detail="pie requires groupby",
                )

        # Table: decide raw vs aggregate
        if actual_viz_type == "table":
            if form_data.get("all_columns"):
                form_data["query_mode"] = "raw"
                form_data.pop("metrics", None)
                form_data.pop("groupby", None)
            else:
                form_data["query_mode"] = "aggregate"
                if not form_data.get("metrics"):
                    raise HTTPException(
                        status_code=400,
                        detail="table aggregate requires metrics or all_columns",
                    )

        # Timeseries-like charts require granularity + metrics
        if actual_viz_type in {
            "echarts_timeseries_line",
            "echarts_timeseries_bar",
            "echarts_timeseries_scatter",
        }:
            if not form_data.get("granularity_sqla"):
                raise HTTPException(
                    status_code=400,
                    detail=f"{actual_viz_type} requires granularity_sqla or time_column",
                )
            if not form_data.get("metrics"):
                raise HTTPException(
                    status_code=400,
                    detail=f"{actual_viz_type} requires metrics",
                )

        chart_payload = {
            "slice_name": payload.slice_name,
            "viz_type": actual_viz_type,
            "datasource_id": payload.dataset_id,
            "datasource_type": payload.datasource_type,
            "params": json.dumps(form_data, ensure_ascii=False),
        }
        if payload.owners is not None:
            chart_payload["owners"] = payload.owners
        if payload.dashboard_id is not None:
            chart_payload["dashboards"] = [payload.dashboard_id]

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
                raise HTTPException(
                    status_code=502,
                    detail=f"Superset API error: {response.text}",
                )
            result = response.json()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

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
            # Some Superset versions return create success without explicit id.
            # Fallback: find by (slice_name, datasource_id, datasource_type).
            page = 0
            page_size = 100
            candidates: list[int] = []
            while page < 10:
                lookup_resp = session.get(
                    f"{base_url}/api/v1/chart/",
                    params={"page": page, "page_size": page_size},
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
                    if str(entry_name or "") != payload.slice_name:
                        continue
                    entry_ds_id = _coerce_int(entry.get("datasource_id"))
                    entry_ds_type = str(entry.get("datasource_type") or "table")
                    if entry_ds_id != payload.dataset_id:
                        continue
                    if entry_ds_type != payload.datasource_type:
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

        return ChartCreateResponse(
            status="created",
            chart_id=chart_id,
            slice_name=payload.slice_name,
            warnings=warnings,
        )

    @app.post("/superset/datasets/query")
    def superset_dataset_query(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        settings = app.state.settings
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        try:
            return fetch_dataset_data(
                settings,
                payload,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

    @app.get("/superset/databases/meta")
    def superset_database_meta() -> dict[str, Any]:
        settings = app.state.settings
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
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
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

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

    @app.get("/superset/databases/{database_id}/tables")
    def superset_database_tables(
        database_id: int,
        schema_name: str = "public",
        limit: int = 500,
    ) -> dict[str, Any]:
        settings = app.state.settings
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        try:
            session = _api_session_with_bearer(settings)
            base_url = _get_base_url(settings)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

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
                raise HTTPException(
                    status_code=502,
                    detail=f"Superset API error: {response.text}",
                )
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

    @app.get("/semantic/schema")
    def cube_meta(
        type: Literal["cubes", "views"] | None = None,
        name: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        settings = app.state.settings
        if not settings.cube_rest_url:
            raise HTTPException(
                status_code=400,
                detail="CUBE_REST_URL not configured",
            )
        try:
            payload = fetch_cube_meta(settings)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Cube API error: {exc}",
            ) from exc
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
            "connectedComponent"
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
        if isinstance(cubes, list):
            filtered: list[dict[str, Any]] = []
            for cube in cubes:
                if not isinstance(cube, dict):
                    continue
                filtered_cube = _strip_keys(cube)
                filtered.append(filtered_cube)
            payload["cubes"] = filtered

        cubes = payload.get("cubes")
        if isinstance(cubes, list) and (type or name):
            type_filter = type
            if type_filter not in {None, "cubes", "views"}:
                raise HTTPException(
                    status_code=400,
                    detail="type must be one of: cubes, views",
                )
            name_filter = name.strip().lower() if isinstance(name, str) else None
            results: list[dict[str, Any]] = []
            for cube in cubes:
                if not isinstance(cube, dict):
                    continue
                if type_filter:
                    cube_type = "views" if str(cube.get("type") or "cube").lower() == "view" else "cubes"
                    if cube_type != type_filter:
                        continue
                if name_filter:
                    cube_name = str(cube.get("name") or "").lower()
                    if name_filter not in cube_name:
                        continue
                results.append(cube)
            return {type_filter or "cubes": results}

        return payload

    @app.post("/semantic/views", response_model=CreateViewResult)
    def semantic_create_view(payload: ViewSpec) -> CreateViewResult:
        settings = app.state.settings
        if not settings.cube_conf_path:
            raise HTTPException(
                status_code=400,
                detail="CUBE_CONF_PATH not configured",
            )
        try:
            result = create_cube_view(settings, payload)
        except ValueError as exc:
            log_unified_event(
                app.state.conn,
                "semantic_view_create_failed",
                {"view_name": payload.view_name, "error": str(exc)},
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log_unified_event(
                app.state.conn,
                "semantic_view_create_failed",
                {"view_name": payload.view_name, "error": str(exc)},
            )
            raise HTTPException(
                status_code=500,
                detail=f"create view failed: {exc}",
            ) from exc
        log_create_view(app.state.conn, payload, result)
        return result

    @app.delete("/semantic/views/{view_name}", response_model=CreateViewResult)
    def semantic_delete_view(view_name: str) -> CreateViewResult:
        settings = app.state.settings
        if not settings.cube_conf_path:
            raise HTTPException(
                status_code=400,
                detail="CUBE_CONF_PATH not configured",
            )
        try:
            result = delete_cube_view(settings, view_name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            log_unified_event(
                app.state.conn,
                "semantic_view_delete_failed",
                {"view_name": view_name, "error": str(exc)},
            )
            raise HTTPException(
                status_code=500,
                detail=f"delete view failed: {exc}",
            ) from exc
        log_unified_event(
            app.state.conn,
            "semantic_view_delete",
            {"view_name": view_name, "result": result.model_dump()},
        )
        return result

    @app.post("/superset/datasets/sync", response_model=SupersetDatasetSyncResult)
    def superset_dataset_sync(
        payload: SupersetDatasetSyncRequest,
        request: Request,
    ) -> SupersetDatasetSyncResult:
        settings = _resolve_superset_settings(request=request, payload=payload)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        try:
            result = sync_superset_dataset(settings, payload)
        except Exception as exc:
            log_unified_event(
                app.state.conn,
                "superset_dataset_sync_failed",
                {
                    "table_name": payload.table_name,
                    "database_id": payload.database_id,
                    "error": str(exc),
                },
            )
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc
        log_dataset_sync(app.state.conn, payload, result)
        return result

    @app.post("/poller/clear", response_model=StatusResponse)
    def poller_clear() -> StatusResponse:
        conn = app.state.conn
        conn.execute("DELETE FROM superset_action_logs")
        db.delete_checkpoint(conn, db.CHECKPOINT_KEY_SUPERSET_LAST_ID)
        return StatusResponse(status="ok")

    @app.get("/superset/users/lookup")
    def superset_user_lookup(username: str) -> dict[str, Any]:
        if not username:
            raise HTTPException(status_code=400, detail="username is required")
        meta_db_uri = app.state.settings.superset_meta_db_uri
        if not meta_db_uri:
            raise HTTPException(status_code=400, detail="SUPERSET_META_DB_URI not set")
        user_id = lookup_user_id(meta_db_uri, username)
        if user_id is None:
            raise HTTPException(status_code=404, detail="user not found")
        return {"username": username, "user_id": user_id}

    def _fetch_dashboard_charts_response(
        settings: Settings,
        dashboard_id: int,
    ) -> dict[str, Any]:
        try:
            charts = fetch_dashboard_charts(settings, dashboard_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc
        return {"dashboard_id": dashboard_id, "count": len(charts), "charts": charts}

    def _resolve_target_user_id(
        settings: Settings,
        user_name: str | None,
        user_id: int | None,
    ) -> int | None:
        if user_name and user_id:
            raise HTTPException(
                status_code=400,
                detail="Provide either user_id or user_name, not both",
            )
        if user_id is not None:
            return user_id
        if not user_name:
            return None
        meta_db_uri = settings.superset_meta_db_uri
        if not meta_db_uri:
            raise HTTPException(status_code=400, detail="SUPERSET_META_DB_URI not set")
        resolved_user_id = lookup_user_id(meta_db_uri, user_name)
        if resolved_user_id is None:
            raise HTTPException(status_code=404, detail="user not found")
        return resolved_user_id

    def _fetch_user_charts(
        settings: Settings,
        target_user_id: int,
        limit: int,
        dashboard_id: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            session = _api_session_with_bearer(settings)
            base_url = _get_base_url(settings)
            _ensure_csrf(session, base_url)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

        page = 0
        page_size = 100
        remaining = max(1, limit)
        charts: list[dict[str, Any]] = []
        while remaining > 0:
            response = session.get(
                f"{base_url}/api/v1/chart/",
                params={"page": page, "page_size": page_size},
                timeout=30,
            )
            if response.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"Superset API error: {response.text}",
                )
            payload = response.json()
            batch = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(batch, list) or not batch:
                break

            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                owners = entry.get("owners") or []
                owner_ids = [
                    owner.get("id")
                    for owner in owners
                    if isinstance(owner, dict) and owner.get("id") is not None
                ]
                if target_user_id not in owner_ids:
                    continue
                dashboards_raw = entry.get("dashboards") or []
                dashboard_ids: list[int] = []
                if isinstance(dashboards_raw, list):
                    for dash in dashboards_raw:
                        if isinstance(dash, dict) and dash.get("id") is not None:
                            try:
                                dashboard_ids.append(int(dash.get("id")))
                            except (TypeError, ValueError):
                                continue
                        elif dash is not None:
                            try:
                                dashboard_ids.append(int(dash))
                            except (TypeError, ValueError):
                                continue
                if dashboard_id is not None and int(dashboard_id) not in dashboard_ids:
                    continue
                charts.append(
                    {
                        "chart_id": entry.get("id") or entry.get("slice_id"),
                        "slice_id": entry.get("slice_id") or entry.get("id"),
                        "name": entry.get("slice_name")
                        or entry.get("name")
                        or entry.get("chart_name"),
                        "viz_type": entry.get("viz_type"),
                        "datasource_id": entry.get("datasource_id")
                        or (entry.get("datasource") or {}).get("id"),
                        "datasource_type": entry.get("datasource_type")
                        or (entry.get("datasource") or {}).get("type"),
                        "dashboard_ids": dashboard_ids,
                        "owners": owners,
                    }
                )
                remaining -= 1
                if remaining <= 0:
                    break
            if len(batch) < page_size:
                break
            page += 1
        return charts

    @app.get("/superset/dashboards/charts")
    def superset_dashboards_charts(
        request: Request,
        dashboard_id: int | None = None,
        user_name: str | None = None,
        # backward-compatible alias
        username: str | None = None,
        user_id: int | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        requested_user_name = user_name or username
        if dashboard_id is None and requested_user_name is None and user_id is None:
            raise HTTPException(
                status_code=400,
                detail="Provide dashboard_id or user_name/user_id",
            )

        if dashboard_id is not None and requested_user_name is None and user_id is None:
            return _fetch_dashboard_charts_response(settings=settings, dashboard_id=dashboard_id)

        target_user_id = _resolve_target_user_id(
            settings=settings,
            user_name=requested_user_name,
            user_id=user_id,
        )
        if target_user_id is None:
            raise HTTPException(
                status_code=400,
                detail="Provide user_name or user_id when dashboard_id is not set",
            )
        charts = _fetch_user_charts(
            settings=settings,
            target_user_id=target_user_id,
            limit=limit,
            dashboard_id=dashboard_id,
        )
        return {
            "dashboard_id": dashboard_id,
            "user_id": target_user_id,
            "user_name": requested_user_name,
            "count": len(charts),
            "charts": charts,
        }

    @app.get("/superset/dashboards/{dashboard_id}/charts")
    def superset_dashboard_charts(dashboard_id: int, request: Request) -> dict[str, Any]:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        return _fetch_dashboard_charts_response(settings=settings, dashboard_id=dashboard_id)

    @app.get("/superset/dashboards/{dashboard_id}/layout")
    def superset_dashboard_layout(dashboard_id: int, request: Request) -> dict[str, Any]:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        try:
            return fetch_dashboard_layout(settings, dashboard_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

    @app.post(
        "/superset/dashboards/{dashboard_id}/layout/append-chart",
        response_model=DashboardAppendChartResponse,
    )
    def superset_dashboard_layout_append_chart(
        dashboard_id: int,
        payload: DashboardAppendChartRequest,
        request: Request,
    ) -> DashboardAppendChartResponse:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        try:
            result = append_chart_to_dashboard_layout(
                settings=settings,
                dashboard_id=dashboard_id,
                chart_id=payload.chart_id,
                tab_id=payload.tab_id,
                tab_name=payload.tab_name,
                width=payload.width,
                height=payload.height,
            )
            return DashboardAppendChartResponse(**result)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

    @app.get("/superset/charts/{chart_id}/data")
    def superset_chart_data(
        chart_id: int,
        request: Request,
        dashboard_id: int | None = None,
        wait_sec: float = 5.0,
        poll_interval_sec: float = 0.5,
    ) -> dict[str, Any]:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        deadline = time.monotonic() + max(wait_sec, 0.0)
        log = None
        while time.monotonic() <= deadline:
            log = _fetch_latest_chart_log(app.state.conn, chart_id)
            if log:
                break
            time.sleep(max(poll_interval_sec, 0.1))
        if not log or not isinstance(log.get("payload"), dict):
            raise HTTPException(status_code=404, detail="chart log not found")
        try:
            response = fetch_chart_data_from_log(
                settings,
                log["payload"],
                dashboard_id=dashboard_id,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc
        data = None
        if isinstance(response, list):
            if response and isinstance(response[0], list):
                data = response[0]
            else:
                data = response
        elif isinstance(response, dict):
            result = response.get("result")
            if isinstance(result, list) and result and isinstance(result[0], dict):
                data = result[0].get("data")
        return {"chart_id": chart_id, "data": data, "raw": response}

    @app.get("/superset/charts/{chart_id}/log-context")
    def superset_chart_log_context(chart_id: int) -> dict[str, Any]:
        conn = app.state.conn
        log = _fetch_latest_chart_log(conn, chart_id)
        if not log:
            raise HTTPException(status_code=404, detail="chart log not found")
        return {"chart_id": chart_id, "log": _summarize_chart_log(log)}

    @app.get("/superset/charts/{chart_id}/form-data")
    def superset_chart_form_data(chart_id: int, request: Request) -> dict[str, Any]:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        try:
            return fetch_chart_form_data(settings, chart_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc

    @app.get("/superset/charts/{chart_id}/queries")
    def superset_chart_queries(chart_id: int, request: Request) -> dict[str, Any]:
        settings = _resolve_superset_settings(request=request)
        if not settings.superset_username or not settings.superset_password:
            raise HTTPException(
                status_code=400,
                detail="Superset credentials not configured",
            )
        if not (settings.superset_internal_url or settings.superset_public_url):
            raise HTTPException(
                status_code=400,
                detail="Superset URL not configured",
            )
        try:
            result = fetch_chart_queries(settings, chart_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc
        if result.get("queries") is None:
            log = _fetch_latest_chart_log(app.state.conn, chart_id)
            payload = log.get("payload") if isinstance(log, dict) else None
            if isinstance(payload, dict):
                result["queries"] = payload.get("queries")
                if result.get("form_data") is None:
                    result["form_data"] = payload.get("form_data")
        return normalize_chart_queries_result(result)

    def _build_superset_active_context(
        conn: duckdb.DuckDBPyConnection,
        settings: Settings,
        dashboard_id: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if dashboard_id is None and session_id:
            dashboard_id = _resolve_session_dashboard_id(session_id)

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

        tab_actions = ("superset_tab_click",)
        figure_actions = (
            "legend_toggle",
            "legend_toggle_activate",
            "legend_toggle_deactivate",
            "chart_click",
            "cross_filter_added",
            "cross_filter_removed",
            "drill_to_detail",
            "drill_by",
            # backward compatibility
            "filter_added",
            "filter_removed",
        )

        def _event_dashboard_id(event: dict[str, Any] | None) -> Any:
            if not isinstance(event, dict):
                return None
            if event.get("dashboard_id") is not None:
                return event.get("dashboard_id")
            payload = _ui_payload(event)
            for key in ("dashboard_id", "dashboardId"):
                value = payload.get(key)
                if value is not None:
                    return value
            return None

        def _event_tab(event: dict[str, Any] | None) -> dict[str, Any] | None:
            payload = _ui_payload(event)
            if not payload:
                return None
            tab_id = payload.get("tab_id") or payload.get("tabId")
            tab_name = payload.get("tab_name") or payload.get("tabName")
            if tab_id is None and tab_name is None:
                return None
            return {"id": tab_id, "name": tab_name}

        def _event_is_fresh(event: dict[str, Any] | None) -> bool:
            if not isinstance(event, dict):
                return False
            if effective_session_id:
                return True
            ui_event_ttl_sec = 30
            dttm = event.get("dttm")
            if not dttm:
                return True
            try:
                event_ts = datetime.fromisoformat(str(dttm))
                if event_ts.tzinfo is None:
                    event_ts = event_ts.replace(tzinfo=timezone.utc)
                age_sec = (datetime.now(timezone.utc) - event_ts).total_seconds()
                return age_sec <= ui_event_ttl_sec
            except Exception:
                return False

        effective_session_id = session_id or _fetch_latest_ui_session_id(
            conn, dashboard_id=dashboard_id
        )
        any_ui_event = _fetch_latest_ui_event(
            conn, dashboard_id=dashboard_id, session_id=effective_session_id
        )
        tab_click_event = _fetch_latest_ui_event(
            conn,
            dashboard_id=dashboard_id,
            session_id=effective_session_id,
            actions=tab_actions,
        )
        figure_event = _fetch_latest_ui_event(
            conn,
            dashboard_id=dashboard_id,
            session_id=effective_session_id,
            actions=figure_actions,
        )
        if not _event_is_fresh(any_ui_event):
            any_ui_event = None
        if not _event_is_fresh(tab_click_event):
            tab_click_event = None
        if not _event_is_fresh(figure_event):
            figure_event = None

        # In session-aware mode, avoid falling back to global chart logs from other users.
        log = (
            None
            if effective_session_id
            else _fetch_latest_active_chart(conn, dashboard_id=dashboard_id)
        )
        if (
            not log
            and not any_ui_event
            and not tab_click_event
            and not figure_event
            and not dashboard_id
        ):
            return {}

        chart_name = None
        effective_dashboard_id = dashboard_id
        if not effective_dashboard_id:
            for event in (figure_event, tab_click_event, any_ui_event):
                event_dashboard_id = _event_dashboard_id(event)
                if event_dashboard_id is not None:
                    effective_dashboard_id = event_dashboard_id
                    break
        if not effective_dashboard_id and log and log.get("dashboard_id"):
            effective_dashboard_id = log.get("dashboard_id")

        charts: list[dict[str, Any]] = []
        if effective_dashboard_id:
            try:
                charts = fetch_dashboard_charts(
                    settings, int(effective_dashboard_id)
                )
            except Exception:
                charts = []

        latest_chart = (
            None
            if effective_session_id
            else _fetch_latest_chart_activity(
                conn, dashboard_id=effective_dashboard_id or dashboard_id
            )
        )
        chart_by_slice: dict[str, dict[str, Any]] = {
            str(chart.get("slice_id")): chart
            for chart in charts
            if chart.get("slice_id") is not None
        }
        active_slice_id = None
        ui_slice_id = _ui_slice_id(figure_event)
        if ui_slice_id is None:
            ui_slice_id = _ui_slice_id(any_ui_event)
        if ui_slice_id is not None:
            active_slice_id = ui_slice_id
        elif latest_chart and latest_chart.get("slice_id"):
            active_slice_id = latest_chart.get("slice_id")
        elif log and log.get("slice_id"):
            active_slice_id = log.get("slice_id")

        active_chart = chart_by_slice.get(str(active_slice_id))
        if active_chart:
            chart_name = active_chart.get("name")

        # "Most recently activated tab" should be primary.
        active_tab = _event_tab(tab_click_event)
        if active_tab is None and active_chart:
            active_tab = active_chart.get("tab")
        if active_tab is None and effective_dashboard_id:
            try:
                active_tab = fetch_dashboard_default_tab(
                    settings, int(effective_dashboard_id)
                )
            except Exception:
                active_tab = None
        if active_tab is None and charts:
            for chart in charts:
                if chart.get("tab"):
                    active_tab = chart.get("tab")
                    break

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
            conn,
            slice_ids=active_slice_ids,
            dashboard_id=int(effective_dashboard_id)
            if effective_dashboard_id is not None
            else None,
        )
        global_filters = _fetch_current_global_filters(
            conn,
            dashboard_id=int(effective_dashboard_id)
            if effective_dashboard_id is not None
            else None,
            session_id=effective_session_id,
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

        response = {
            "dashboard_id": log.get("dashboard_id") if log else effective_dashboard_id,
            "active_tab": active_tab,
            "active_charts": active_charts,
            "session_id": effective_session_id,
        }
        return {key: value for key, value in response.items() if value is not None}

    @app.get("/superset/charts/active")
    def superset_active_chart(
        request: Request,
        dashboard_id: int | None = None,
        session_id: str | None = None,
        user_key: str | None = None,
    ) -> dict[str, Any]:
        poller = app.state.superset_poller
        if poller:
            try:
                asyncio.run(poller.poll_once())
            except RuntimeError:
                # If we're already in an event loop, skip sync poll.
                pass
            try:
                app.state.writer.flush_blocking(timeout=2.0)
            except Exception:
                pass
        effective_user_key = _resolve_effective_user_key(user_key, session_id)
        existing_entry = app.state.db_store.get_existing(effective_user_key)
        if existing_entry:
            _existing_conn, existing_writer, _path = existing_entry
            try:
                existing_writer.flush_blocking(timeout=2.0)
            except Exception:
                pass
        conn = _connect_logs_for_read(user_key=user_key, session_id=session_id)
        settings = _resolve_superset_settings(request=request)
        try:
            return _build_superset_active_context(
                conn, settings=settings, dashboard_id=dashboard_id, session_id=session_id
            )
        finally:
            conn.close()

    @app.get("/superset/charts/active/stream")
    async def superset_active_chart_stream(
        request: Request,
        dashboard_id: int | None = None,
        session_id: str | None = None,
        user_key: str | None = None,
        poll_interval_sec: float = 1.0,
    ) -> StreamingResponse:
        settings = _resolve_superset_settings(request=request)
        async def event_generator() -> Any:
            last_payload = ""
            while True:
                if await request.is_disconnected():
                    break
                poller = app.state.superset_poller
                if poller:
                    try:
                        await poller.poll_once()
                        app.state.writer.flush_blocking(timeout=2.0)
                    except Exception:
                        pass
                effective_user_key = _resolve_effective_user_key(user_key, session_id)
                existing_entry = app.state.db_store.get_existing(effective_user_key)
                if existing_entry:
                    _existing_conn, existing_writer, _path = existing_entry
                    try:
                        existing_writer.flush_blocking(timeout=2.0)
                    except Exception:
                        pass
                conn = _connect_logs_for_read(user_key=user_key, session_id=session_id)
                try:
                    payload = _build_superset_active_context(
                        conn, settings=settings, dashboard_id=dashboard_id, session_id=session_id
                    )
                finally:
                    conn.close()
                payload_text = json.dumps(payload, default=str, ensure_ascii=False)
                if payload_text != last_payload:
                    last_payload = payload_text
                    yield f"data: {payload_text}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(poll_interval_sec)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )


    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        payload: ChatRequest,
        request: Request,
    ) -> ChatResponse:
        request_id = uuid.uuid4().hex
        start = time.perf_counter()
        conn, writer, _ = await app.state.db_store.get_or_create(payload.user_id)
        settings = _resolve_superset_settings(request=request, payload=payload)
        active_context = _build_superset_active_context(
            conn,
            settings=settings,
            dashboard_id=payload.dashboard_id,
            session_id=payload.session_id,
        )
        if payload.mask_active_state_for_evaluation:
            active_context = {
                "dashboard_id": payload.dashboard_id,
                "session_id": payload.session_id,
                "active_tab": None,
                "active_charts": [],
                "state_masked_for_evaluation": True,
            }

        plan = {
            "measures": [],
            "dimensions": [],
            "time_range": [],
        }
        data: list[dict[str, Any]] = []
        trace_logs = _load_recent_trace_logs(
            conn,
            session_id=payload.session_id,
            dashboard_id=payload.dashboard_id,
            limit=80,
        )
        if payload.mask_active_state_for_evaluation:
            trace_logs = []
        agent_runner = app.state.agent_runner
        await agent_runner.startup()
        request_history = _normalize_request_history(payload.history, limit=20)
        history_for_agent = request_history
        if not history_for_agent:
            history_for_agent = _load_dialogue_history(conn, payload.session_id, 20)
        active_charts_context = _build_active_charts_prompt_context(
            conn, active_context, limit=8
        )
        aer_candidates = app.state.aer_store.refresh_snapshot(
            active_context, active_charts_context
        )
        chart_context = json.dumps(
            {
                "active_context": active_context,
                "active_charts_context": active_charts_context,
                "aer_candidates": aer_candidates,
                "trace_logs": trace_logs,
                "verified_ui_evidence": (
                    {} if payload.mask_active_state_for_evaluation else payload.verified_ui_evidence
                ),
            },
            ensure_ascii=False,
        )
        chart_context_obj = AgentContext(
            settings=settings,
            conn=conn,
            trace_logs=trace_logs,
            active_chart=active_context,
            aer_candidates=aer_candidates,
            mask_active_state_for_evaluation=payload.mask_active_state_for_evaluation,
        )
        app.state.last_chat_context = {
            "active_context": active_context,
            "active_charts_context": active_charts_context,
            "aer_candidates": aer_candidates,
            "verified_ui_evidence": (
                {} if payload.mask_active_state_for_evaluation else payload.verified_ui_evidence
            ),
        }
        app.state.context_cleared = False
        debug_items: list[dict[str, Any]] = []
        if payload.debug:
            debug_items.append(
                {
                    "type": "context",
                    "active_context": active_context,
                    "active_charts_context": active_charts_context,
                    "aer_candidates": aer_candidates,
                    "active_state_masked_for_evaluation": payload.mask_active_state_for_evaluation,
                }
            )
        answer, agent_debug_items, raw_output = await agent_runner.respond(
            payload.message,
            history_for_agent,
            context=chart_context,
            context_obj=chart_context_obj,
            debug=True,
            model_name=payload.agent_model,
        )
        if payload.debug and agent_debug_items:
            debug_items.extend(agent_debug_items)
        if payload.debug:
            app.state.last_chat_debug = {
                "session_id": payload.session_id,
                "request_id": request_id,
                "items": debug_items,
            }
        else:
            app.state.last_chat_debug = None

        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = _estimate_token_usage(payload.message, answer)
        usd_cost = _estimate_usd_cost(
            usage["prompt_tokens"], usage["completion_tokens"]
        )
        chat_payload = DuckDBWriter.build_streamlit_chat_payload(
            session_id=payload.session_id,
            request_id=request_id,
            user_id=payload.user_id,
            message=payload.message,
            response=answer,
            response_raw=raw_output,
            response_events=_extract_response_events(agent_debug_items),
            latency_ms=latency_ms,
            model_name=(payload.agent_model or os.getenv("AGENT_MODEL", "gpt-5-mini")),
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            token_cost=usage["total_tokens"],
            usd_cost=usd_cost,
        )
        await writer.enqueue_streamlit_chat_log(chat_payload)

        return ChatResponse(
            session_id=payload.session_id,
            request_id=request_id,
            answer=answer,
            query_plan=plan,
            data=data,
            debug=debug_items if payload.debug else None,
        )

    @app.post("/evaluation/dashboard-only", response_model=DashboardOnlyResponse)
    async def dashboard_only_evaluation(
        payload: DashboardOnlyRequest,
    ) -> DashboardOnlyResponse:
        """Answer from manually replayed visible dashboard evidence without AER/tools."""
        model = (payload.model or os.getenv("AGENT_MODEL", "gpt-5-mini")).strip()
        reasoning_effort = os.getenv("AGENT_REASONING_EFFORT", "medium").strip().lower()
        if reasoning_effort not in {"low", "medium", "high"}:
            reasoning_effort = "medium"
        service_tier = os.getenv("OPENAI_SERVICE_TIER", "").strip()
        request_kwargs: dict[str, Any] = {
            "model": model,
            "response_format": {"type": "json_object"},
            "reasoning_effort": reasoning_effort,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a dashboard-only analyst. Answer using only the supplied visible "
                        "dashboard evidence. You have no tools, hidden state, logs, database access, "
                        "or prior conversation. Return only the task's requested JSON structure."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Task:\n{payload.message}\n\nVisible dashboard evidence:\n{payload.visible_evidence}",
                },
            ],
        }
        if service_tier:
            request_kwargs["service_tier"] = service_tier
        completion = await asyncio.to_thread(
            OpenAI().chat.completions.create,
            **request_kwargs,
        )
        answer = (completion.choices[0].message.content or "").strip()
        return DashboardOnlyResponse(
            answer=answer,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    @app.post("/chat/stream")
    async def chat_stream(
        payload: ChatRequest,
        request: Request,
    ) -> StreamingResponse:
        request_id = uuid.uuid4().hex
        agent_runner = app.state.agent_runner
        await agent_runner.startup()
        chart_context = None
        chart_context_obj = None
        conn, writer, _ = await app.state.db_store.get_or_create(payload.user_id)
        request_history = _normalize_request_history(payload.history, limit=20)
        history_for_agent = request_history
        if not history_for_agent:
            history_for_agent = _load_dialogue_history(conn, payload.session_id, 20)
        settings = _resolve_superset_settings(request=request, payload=payload)
        active_context = _build_superset_active_context(
            conn,
            settings=settings,
            dashboard_id=payload.dashboard_id,
            session_id=payload.session_id,
        )
        if payload.mask_active_state_for_evaluation:
            active_context = {
                "dashboard_id": payload.dashboard_id,
                "session_id": payload.session_id,
                "active_tab": None,
                "active_charts": [],
                "state_masked_for_evaluation": True,
            }
        trace_logs = _load_recent_trace_logs(
            conn,
            session_id=payload.session_id,
            dashboard_id=payload.dashboard_id,
            limit=80,
        )
        if payload.mask_active_state_for_evaluation:
            trace_logs = []
        active_charts_context = _build_active_charts_prompt_context(
            conn, active_context, limit=8
        )
        aer_candidates = app.state.aer_store.refresh_snapshot(
            active_context, active_charts_context
        )
        chart_context = json.dumps(
            {
                "active_context": active_context,
                "active_charts_context": active_charts_context,
                "aer_candidates": aer_candidates,
                "trace_logs": trace_logs,
                "verified_ui_evidence": (
                    {} if payload.mask_active_state_for_evaluation else payload.verified_ui_evidence
                ),
            },
            ensure_ascii=False,
        )
        chart_context_obj = AgentContext(
            settings=settings,
            conn=conn,
            trace_logs=trace_logs,
            active_chart=active_context,
            aer_candidates=aer_candidates,
            mask_active_state_for_evaluation=payload.mask_active_state_for_evaluation,
        )
        app.state.last_chat_context = {
            "active_context": active_context,
            "active_charts_context": active_charts_context,
            "aer_candidates": aer_candidates,
            "verified_ui_evidence": (
                {} if payload.mask_active_state_for_evaluation else payload.verified_ui_evidence
            ),
        }
        app.state.context_cleared = False

        async def event_generator() -> Any:
            final_answer = None
            final_raw = None
            response_events: list[dict[str, Any]] = []
            start = time.perf_counter()
            async for event in agent_runner.respond_stream(
                payload.message,
                history_for_agent,
                context=chart_context,
                context_obj=chart_context_obj,
                debug=payload.debug,
                model_name=payload.agent_model,
            ):
                if event.get("event") == "final":
                    final_answer = event.get("answer")
                    final_raw = event.get("raw")
                if event.get("event") in ("tool_called", "tool_output"):
                    response_events.append(event)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            if final_answer is None:
                final_answer = "No answer."
            usage = _estimate_token_usage(payload.message, final_answer)
            usd_cost = _estimate_usd_cost(
                usage["prompt_tokens"], usage["completion_tokens"]
            )

            chat_payload = DuckDBWriter.build_streamlit_chat_payload(
                session_id=payload.session_id,
                request_id=request_id,
                user_id=payload.user_id,
                message=payload.message,
                response=final_answer,
                response_raw=final_raw,
                response_events=response_events,
                latency_ms=int((time.perf_counter() - start) * 1000),
                model_name=(payload.agent_model or os.getenv("AGENT_MODEL", "gpt-5-mini")),
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                token_cost=usage["total_tokens"],
                usd_cost=usd_cost,
            )
            await writer.enqueue_streamlit_chat_log(chat_payload)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/chat/context/latest")
    def chat_context_latest() -> dict[str, Any]:
        ctx = app.state.last_chat_context
        if app.state.context_cleared:
            return {"context": None}
        if ctx:
            return {"context": ctx}
        conn = app.state.conn
        fallback_active_context = _build_superset_active_context(
            conn,
            settings=app.state.settings,
            dashboard_id=None,
            session_id=None,
        )
        if not isinstance(fallback_active_context, dict) or not fallback_active_context:
            return {"context": None}
        active_charts_context = _build_active_charts_prompt_context(
            conn, fallback_active_context, limit=8
        )
        aer_candidates = app.state.aer_store.refresh_snapshot(
            fallback_active_context, active_charts_context
        )
        return {
            "context": {
                "active_context": fallback_active_context,
                "active_charts_context": active_charts_context,
                "aer_candidates": aer_candidates,
            }
        }

    @app.get("/chat/debug/latest")
    def chat_debug_latest() -> dict[str, Any]:
        dbg = app.state.last_chat_debug
        if not dbg:
            return {"debug": None}
        return {"debug": dbg}

    @app.get("/chat/session_status")
    def chat_session_status(
        request: Request,
        session_id: str,
        message: str | None = None,
        since_ts: str | None = None,
        user_key: str | None = None,
    ) -> dict[str, Any]:
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        header_user_key = _normalize_user_key(
            request.headers.get("X-Superset-Username")
        )
        effective_user_key = _resolve_effective_user_key(
            user_key or header_user_key, session_id
        )
        conn = _connect_logs_for_read(effective_user_key, session_id)
        normalized_message = (
            message.strip() if isinstance(message, str) and message.strip() else None
        )
        normalized_since_ts = (
            since_ts.strip() if isinstance(since_ts, str) and since_ts.strip() else None
        )
        try:
            filters = ["session_id = ?"]
            params: list[Any] = [session_id]
            if normalized_message:
                filters.append("message = ?")
                params.append(normalized_message)
            if normalized_since_ts:
                filters.append("ts >= CAST(? AS TIMESTAMP)")
                params.append(normalized_since_ts)
            where_clause = " AND ".join(filters)
            row = conn.execute(
                f"""
                SELECT ts, request_id, message, response, response_raw, latency_ms
                FROM streamlit_chat_logs
                WHERE {where_clause}
                ORDER BY ts DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            latest_row = conn.execute(
                """
                SELECT ts, request_id, message, response
                FROM streamlit_chat_logs
                WHERE session_id = ?
                ORDER BY ts DESC
                LIMIT 1
                """,
                [session_id],
            ).fetchone()
        finally:
            conn.close()

        if row:
            response = row[3] or ""
            return {
                "session_id": session_id,
                "status": "complete" if str(response).strip() else "running",
                "has_final_answer": bool(str(response).strip()),
                "request_id": row[1],
                "message": row[2],
                "answer": response,
                "answer_head": str(response)[:300],
                "response_raw_head": str(row[4] or "")[:300],
                "ts": row[0].isoformat() if row[0] else None,
                "latency_ms": row[5],
            }

        latest = None
        if latest_row:
            latest = {
                "ts": latest_row[0].isoformat() if latest_row[0] else None,
                "request_id": latest_row[1],
                "message": latest_row[2],
                "answer_head": str(latest_row[3] or "")[:300],
            }
        return {
            "session_id": session_id,
            "status": "running" if latest_row else "missing",
            "has_final_answer": False,
            "message": normalized_message,
            "latest": latest,
        }

    @app.get("/chat/dialogue")
    def chat_dialogue(
        request: Request,
        session_id: str,
        limit: int = 50,
        user_key: str | None = None,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be > 0")
        header_user_key = _normalize_user_key(
            request.headers.get("X-Superset-Username")
        )
        effective_user_key = _resolve_effective_user_key(
            user_key or header_user_key, session_id
        )
        conn = _connect_logs_for_read(effective_user_key, session_id)
        try:
            rows = conn.execute(
                """
                SELECT ts, user_id, message, response, response_raw, response_events
                FROM streamlit_chat_logs
                WHERE session_id = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                [session_id, limit],
            ).fetchall()
        finally:
            conn.close()
        dialogue = []
        for ts, user_id, message, response, response_raw, response_events in reversed(rows):
            if message:
                dialogue.append(
                    {
                        "role": "user",
                        "content": message,
                        "ts": ts.isoformat() if ts else None,
                        "user_id": user_id,
                    }
                )
            if response_events:
                try:
                    events = json.loads(response_events)
                except json.JSONDecodeError:
                    events = []
                if isinstance(events, list):
                    for event in events:
                        dialogue.append(
                            {
                                "role": "assistant",
                                "type": "event",
                                "event": event,
                                "content": "",
                                "ts": ts.isoformat() if ts else None,
                                "user_id": user_id,
                            }
                        )
            if response:
                item = {
                    "role": "assistant",
                    "content": response,
                    "ts": ts.isoformat() if ts else None,
                    "user_id": user_id,
                }
                if response_raw:
                    item["content_raw"] = response_raw
                dialogue.append(item)
        return {"session_id": session_id, "dialogue": dialogue}

    def _extract_response_events(
        items: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if not items:
            return []
        response_events = []
        for item in items:
            item_type = item.get("type")
            if item_type in ("tool_call_item", "tool_call_output_item"):
                response_events.append(item)
        return response_events

    @app.delete("/chat/dialogue")
    def clear_chat_dialogue(
        request: Request,
        session_id: str,
        user_key: str | None = None,
    ) -> StatusResponse:
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        header_user_key = _normalize_user_key(
            request.headers.get("X-Superset-Username")
        )
        effective_user_key = _resolve_effective_user_key(
            user_key or header_user_key, session_id
        )
        session_user_key = _resolve_session_user_key(session_id)
        base_path = app.state.settings.duckdb_path
        candidate_paths: set[str] = {base_path}
        for key in (effective_user_key, session_user_key):
            if key:
                candidate_paths.add(db.resolve_user_duckdb_path(base_path, key))
        for path in candidate_paths:
            if not Path(path).exists():
                continue
            conn = db.connect(path, read_only=False)
            try:
                conn.execute(
                    "DELETE FROM streamlit_chat_logs WHERE session_id = ?",
                    [session_id],
                )
            finally:
                conn.close()
        return StatusResponse(status="cleared")

    @app.delete("/chat/context")
    def clear_chat_context() -> StatusResponse:
        app.state.last_chat_context = None
        app.state.aer_store.clear()
        app.state.context_cleared = True
        return StatusResponse(status="cleared")

    @app.post("/events", response_model=StatusResponse)
    async def events(payload: EventRequest) -> StatusResponse:
        session_id = _normalize_user_key(payload.session_id)
        payload_body = payload.payload if isinstance(payload.payload, dict) else {}
        event_dashboard_id = _extract_dashboard_id(payload_body)
        event_user_key = _normalize_user_key(payload.user_id)
        identified_username = None
        identified_superset_user_id = None
        if payload.event_type == "superset_user_identified":
            identified_username, identified_superset_user_id = _extract_superset_identity(
                payload_body
            )
            _upsert_session_meta(
                session_id=session_id,
                username=identified_username or event_user_key,
                superset_user_id=identified_superset_user_id,
                dashboard_id=event_dashboard_id,
            )
        session_user_key = _resolve_session_user_key(session_id)
        effective_user_key = session_user_key or identified_username or event_user_key
        if event_dashboard_id is None:
            event_dashboard_id = _resolve_session_dashboard_id(session_id)
        if event_dashboard_id is None:
            event_dashboard_id = _resolve_dashboard_id_from_user_logs(
                user_key=effective_user_key or event_user_key,
                session_id=session_id,
            )
        if session_id and (effective_user_key or event_dashboard_id is not None):
            _upsert_session_meta(
                session_id=session_id,
                username=effective_user_key,
                dashboard_id=event_dashboard_id,
            )
        event_id = time.time_ns()
        event_ts = payload.ts or datetime.now(timezone.utc)
        payload_for_log = dict(payload_body)
        if effective_user_key:
            payload_for_log.setdefault("resolved_user_key", effective_user_key)
        if event_dashboard_id is not None:
            payload_for_log.setdefault("dashboard_id", event_dashboard_id)
        if payload.event_type == "superset_user_identified":
            if identified_username:
                payload_for_log["username"] = identified_username
            if identified_superset_user_id is not None:
                payload_for_log["superset_user_id"] = identified_superset_user_id
        _conn, writer, _ = await app.state.db_store.get_or_create(effective_user_key)
        ui_payload = DuckDBWriter.build_ui_payload(
            event_id=event_id,
            ts=event_ts,
            session_id=payload.session_id,
            user_id=effective_user_key or event_user_key,
            event_type=payload.event_type,
            payload=payload_for_log,
        )
        await writer.enqueue_ui_event(ui_payload)
        return StatusResponse(status="ok")

    return app


app = create_app()
