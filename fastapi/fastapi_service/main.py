from __future__ import annotations
import duckdb
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from fastapi_service import db
from fastapi_service.config import Settings, load_settings
from fastapi_service.agent import AgentRunner, AgentContext
from fastapi_service.models import ChatRequest, ChatResponse, EventRequest, StatusResponse
from fastapi_service.superset import (
    QueryTranslater,
    SupersetPoller,
    fetch_dashboard_charts,
    fetch_dataset_schema,
    fetch_dashboard_layout,
    extract_tab_map,
    lookup_user_id,
    fetch_chart_data_from_log,
)
from fastapi_service.cube import fetch_cube_meta
from fastapi_service.cube_conf import load_repo_schema
from fastapi_service.writer import DuckDBWriter

logger = logging.getLogger(__name__)



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
        WHERE slice_id = ? AND action IN ('ChartDataRestApi.data', 'ChartDataRestApi.json_dumps')
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

    latest_row = conn.execute(
        """
        SELECT superset_log_id, dttm, action, dashboard_id, slice_id, json
        FROM superset_action_logs
        ORDER BY superset_log_id DESC
        LIMIT 1
        """
    ).fetchone()
    if latest_row:
        latest_action = latest_row[2]
        if latest_action == "DashboardRestApi.get":
            return None
        if latest_action in ("ChartDataRestApi.data", "ChartDataRestApi.json_dumps"):
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


def _summarize_chart_log(log: dict[str, Any] | None) -> dict[str, Any]:
    if not log or not isinstance(log, dict):
        return {}
    payload = log.get("payload") or {}
    form_data = payload.get("form_data") if isinstance(payload, dict) else None
    queries = payload.get("queries") if isinstance(payload, dict) else None
    datasource = payload.get("datasource") if isinstance(payload, dict) else None
    return {
        "superset_log_id": log.get("superset_log_id"),
        "slice_id": log.get("slice_id"),
        "dashboard_id": log.get("dashboard_id"),
        "form_data": form_data,
        "queries": queries,
        "datasource": datasource,
    }


def _decorate_superset_log(row: dict[str, Any]) -> dict[str, Any]:
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
        event_name = payload.get("event_name")
        if event_name:
            row["event_name"] = event_name
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
    conn = db.connect(settings.duckdb_path)
    db.init_schema(conn)
    writer = DuckDBWriter(conn)
    await writer.start()
    app.state.conn = conn
    app.state.writer = writer
    app.state.superset_poller = None
    app.state.superset_task = None

    if settings.superset_meta_db_uri:
        poller = SupersetPoller(
            meta_db_uri=settings.superset_meta_db_uri,
            poll_interval_sec=settings.superset_poll_interval_sec,
            batch_size=settings.superset_batch_size,
            dashboard_id=settings.superset_log_dashboard_id,
            user_id=settings.superset_log_user_id,
            username=settings.superset_log_username,
            writer=writer,
            conn=conn,
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
        writer = app.state.writer
        await writer.stop()
        conn.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Agent4OLAP FastAPI", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = settings
    app.state.agent_runner = AgentRunner()
    app.state.last_chat_context = None
    app.state.last_chat_debug = None
    app.state.context_cleared = False

    def get_writer() -> DuckDBWriter:
        return app.state.writer

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

    @app.get("/superset/logs/latest")
    def superset_logs_latest(
        dashboard_id: int | None = None,
        user_id: int | None = None,
        action: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conn = db.connect(app.state.settings.duckdb_path, read_only=False)
        try:
            filters = []
            params: list[Any] = []
            if dashboard_id is not None:
                filters.append("dashboard_id = ?")
                params.append(dashboard_id)
            if user_id is not None:
                filters.append("user_id = ?")
                params.append(user_id)
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
        action: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        conn = db.connect(app.state.settings.duckdb_path, read_only=False)
        try:
            filters = ["superset_log_id > ?"]
            params: list[Any] = [last_id]
            if dashboard_id is not None:
                filters.append("dashboard_id = ?")
                params.append(dashboard_id)
            if user_id is not None:
                filters.append("user_id = ?")
                params.append(user_id)
            if action is not None:
                filters.append("action = ?")
                params.append(action)
            where_clause = " AND ".join(filters)
            sql = (
                "SELECT superset_log_id, dttm, action, user_id, dashboard_id, "
                "slice_id, duration_ms, referrer, json, ingested_at "
                "FROM superset_action_logs "
                f"WHERE {where_clause} "
                "ORDER BY superset_log_id ASC "
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

    @app.get("/superset/logs/stream")
    async def superset_logs_stream(
        request: Request,
        dashboard_id: int | None = None,
        user_id: int | None = None,
        action: str | None = None,
        last_id: int = 0,
        limit: int = 100,
        poll_interval_sec: float = 1.0,
    ) -> StreamingResponse:
        translator = QueryTranslater(settings=app.state.settings)
        header_last_id = request.headers.get("Last-Event-ID") or request.headers.get(
            "last-event-id"
        )
        if header_last_id:
            try:
                last_id = max(last_id, int(header_last_id))
            except ValueError:
                pass

        async def event_generator() -> Any:
            current_id = last_id
            while True:
                if await request.is_disconnected():
                    break
                try:
                    rows = await asyncio.to_thread(
                        _fetch_superset_logs_since,
                        last_id=current_id,
                        dashboard_id=dashboard_id,
                        user_id=user_id,
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
                    current_id = max(current_id, row_id)
                    if (
                        row.get("action") in QueryTranslater.SUPPORTED_ACTIONS
                        and row.get("json")
                    ):
                        try:
                            parsed = translator.parse_payload(row["json"])
                        except Exception as exc:
                            logger.exception("Superset SQL translate failed: %s", exc)
                            parsed = None
                        if parsed:
                            row["translated_sql"] = parsed.get("sql")
                            row["translated_filters"] = parsed.get("filters")
                            row["translated_where"] = parsed.get("where")
                    payload = json.dumps(row, default=str)
                    yield f"id: {row_id}\n" f"data: {payload}\n\n"
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(poll_interval_sec)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    @app.get("/superset/logs/latest_sql")
    def superset_logs_latest_sql(
        dashboard_id: int | None = None,
        slice_id: int | None = None,
    ) -> dict[str, Any]:
        conn = db.connect(app.state.settings.duckdb_path, read_only=False)
        try:
            filters = [
                "action IN ('ChartDataRestApi.data','ChartDataRestApi.json_dumps')"
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
    def superset_dataset_schema(dataset_id: int) -> dict[str, Any]:
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
            schema = fetch_dataset_schema(settings, dataset_id)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc
        return schema

    @app.get("/cube/meta")
    def cube_meta() -> dict[str, Any]:
        settings = app.state.settings
        if not settings.cube_rest_url:
            raise HTTPException(
                status_code=400,
                detail="CUBE_REST_URL not configured",
            )
        try:
            return fetch_cube_meta(settings)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Cube API error: {exc}",
            ) from exc

    @app.get("/cube/schema")
    def cube_schema() -> dict[str, Any]:
        settings = app.state.settings
        if not settings.cube_conf_path:
            raise HTTPException(
                status_code=400,
                detail="CUBE_CONF_PATH not configured",
            )
        try:
            return load_repo_schema(settings.cube_conf_path)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Cube conf parse error: {exc}",
            ) from exc

    def _fetch_ui_events_since(
        *,
        last_id: int,
        session_id: str | None,
        event_type: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        conn = db.connect(app.state.settings.duckdb_path, read_only=False)
        try:
            filters = ["event_id > ?"]
            params: list[Any] = [last_id]
            if session_id is not None:
                filters.append("session_id = ?")
                params.append(session_id)
            if event_type is not None:
                filters.append("event_type = ?")
                params.append(event_type)
            where_clause = " AND ".join(filters)
            sql = (
                "SELECT event_id, ts, session_id, user_id, event_type, payload_json "
                "FROM ui_events "
                f"WHERE {where_clause} "
                "ORDER BY event_id ASC "
                "LIMIT ?"
            )
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [
            {
                "event_id": row[0],
                "ts": row[1],
                "session_id": row[2],
                "user_id": row[3],
                "event_type": row[4],
                "payload": json.loads(row[5]) if row[5] else {},
                "source": "ui",
            }
            for row in rows
        ]

    @app.get("/events/stream")
    async def ui_events_stream(
        request: Request,
        session_id: str | None = None,
        event_type: str | None = None,
        last_id: int = 0,
        limit: int = 100,
        poll_interval_sec: float = 1.0,
    ) -> StreamingResponse:
        header_last_id = request.headers.get("Last-Event-ID") or request.headers.get(
            "last-event-id"
        )
        if header_last_id:
            try:
                last_id = max(last_id, int(header_last_id))
            except ValueError:
                pass

        async def event_generator() -> Any:
            current_id = last_id
            while True:
                if await request.is_disconnected():
                    break
                rows = await asyncio.to_thread(
                    _fetch_ui_events_since,
                    last_id=current_id,
                    session_id=session_id,
                    event_type=event_type,
                    limit=limit,
                )
                for row in rows:
                    row_id = int(row["event_id"] or 0)
                    current_id = max(current_id, row_id)
                    payload = json.dumps(row, default=str)
                    yield f"id: {row_id}\n" f"data: {payload}\n\n"
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(poll_interval_sec)

        return StreamingResponse(event_generator(), media_type="text/event-stream")

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

    @app.get("/superset/dashboards/{dashboard_id}/charts")
    def superset_dashboard_charts(dashboard_id: int) -> dict[str, Any]:
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
        return {"dashboard_id": dashboard_id, "charts": charts}

    @app.get("/superset/dashboards/{dashboard_id}/tab-map")
    def superset_dashboard_tab_map(dashboard_id: int) -> dict[str, Any]:
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
            layout = fetch_dashboard_layout(settings, dashboard_id)
            tab_map = extract_tab_map(layout.get("layout") or {})
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Superset API error: {exc}",
            ) from exc
        return {"dashboard_id": dashboard_id, "tab_map": tab_map}

    @app.get("/superset/charts/{chart_id}/data")
    def superset_chart_data(
        chart_id: int,
        dashboard_id: int | None = None,
        wait_sec: float = 5.0,
        poll_interval_sec: float = 0.5,
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
                app.state.settings,
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

    @app.get("/superset/charts/active")
    def superset_active_chart(
        dashboard_id: int | None = None,
    ) -> dict[str, Any]:
        conn = app.state.conn
        log = _fetch_latest_active_chart(conn, dashboard_id=dashboard_id)
        if not log:
            return {"slice_id": None, "superset_log_id": None, "dashboard_id": dashboard_id}
        chart_name = None
        if log.get("slice_id"):
            try:
                charts = fetch_dashboard_charts(app.state.settings, int(log.get("dashboard_id") or dashboard_id or 0))
            except Exception:
                charts = []
            for chart in charts:
                if str(chart.get("slice_id")) == str(log.get("slice_id")):
                    chart_name = chart.get("name")
                    break
        return {
            "slice_id": log.get("slice_id"),
            "superset_log_id": log.get("superset_log_id"),
            "dashboard_id": log.get("dashboard_id"),
            "chart_name": chart_name,
            "action": log.get("action"),
            "dttm": log.get("dttm"),
        }


    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        payload: ChatRequest, writer: DuckDBWriter = Depends(get_writer)
    ) -> ChatResponse:
        request_id = uuid.uuid4().hex
        start = time.perf_counter()
        conn = app.state.conn
        if payload.active_chart_id is None:
            latest = _fetch_latest_active_chart(conn)
            if latest:
                payload.active_chart_id = latest.get("slice_id")

        plan = {
            "measures": [],
            "dimensions": [],
            "time_range": [],
        }
        data: list[dict[str, Any]] = []
        agent_runner = app.state.agent_runner
        await agent_runner.startup()
        chart_context = None
        chart_context_obj = None
        debug_items: list[dict[str, Any]] = []
        if payload.active_chart_id:
            log = _fetch_latest_chart_log(conn, payload.active_chart_id)
            chart_data = None
            if log and isinstance(log.get("payload"), dict):
                try:
                    response = fetch_chart_data_from_log(
                        app.state.settings,
                        log["payload"],
                    )
                    chart_data = _summarize_chart_data(response)
                except Exception as exc:
                    chart_data = {"chart_data": "unavailable"}
                    if payload.debug:
                        chart_data["error"] = str(exc)
            summary = _summarize_chart_log(log) if log else {"chart_log": "unavailable"}
            chart_context = json.dumps(
                {
                    "active_chart_id": payload.active_chart_id,
                    "active_chart_name": payload.active_chart_name,
                    "chart_log": summary,
                    "chart_data": chart_data,
                },
                ensure_ascii=False,
            )
            if payload.debug:
                debug_items.append(
                    {
                        "type": "context",
                        "active_chart_id": payload.active_chart_id,
                        "active_chart_name": payload.active_chart_name,
                        "chart_log": summary,
                        "chart_data": chart_data,
                    }
                )
            chart_context_obj = AgentContext(
                settings=app.state.settings,
                conn=app.state.conn,
                chart_id=payload.active_chart_id,
                chart_name=payload.active_chart_name,
                chart_data=chart_data,
            )
            app.state.last_chat_context = {
                "chart_id": payload.active_chart_id,
                "chart_name": payload.active_chart_name,
                "chart_data": chart_data,
                "chart_log": summary,
            }
            app.state.context_cleared = False
        else:
            app.state.last_chat_context = None
            app.state.context_cleared = False
            chart_context_obj = AgentContext(
                settings=app.state.settings,
                conn=conn,
            )
        answer, agent_debug_items = await agent_runner.respond(
            payload.message,
            payload.history or [],
            context=chart_context,
            context_obj=chart_context_obj,
            debug=payload.debug,
        )
        if payload.debug and agent_debug_items:
            debug_items.extend(agent_debug_items)
        if payload.active_chart_id or payload.active_chart_name:
            chart_name = payload.active_chart_name or "Unknown"
            chart_id = payload.active_chart_id
            if chart_id is not None:
                prefix = f"[Chart {chart_name} (id={chart_id})]"
            else:
                prefix = f"[Chart {chart_name}]"
            answer = f"{prefix}{answer}"
        if payload.debug:
            app.state.last_chat_debug = {
                "session_id": payload.session_id,
                "request_id": request_id,
                "items": debug_items,
            }
        else:
            app.state.last_chat_debug = None

        latency_ms = int((time.perf_counter() - start) * 1000)
        chat_payload = DuckDBWriter.build_streamlit_chat_payload(
            session_id=payload.session_id,
            request_id=request_id,
            user_id=payload.user_id,
            message=payload.message,
            response=answer,
            latency_ms=latency_ms,
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

    @app.post("/chat/stream")
    async def chat_stream(
        payload: ChatRequest, writer: DuckDBWriter = Depends(get_writer)
    ) -> StreamingResponse:
        request_id = uuid.uuid4().hex
        agent_runner = app.state.agent_runner
        await agent_runner.startup()
        chart_context = None
        chart_context_obj = None
        conn = app.state.conn
        if payload.active_chart_id is None:
            latest = _fetch_latest_active_chart(conn)
            if latest:
                payload.active_chart_id = latest.get("slice_id")

        if payload.active_chart_id:
            log = _fetch_latest_chart_log(conn, payload.active_chart_id)
            chart_data = None
            if log and isinstance(log.get("payload"), dict):
                try:
                    response = fetch_chart_data_from_log(
                        app.state.settings,
                        log["payload"],
                    )
                    chart_data = _summarize_chart_data(response)
                except Exception:
                    chart_data = {"chart_data": "unavailable"}
            summary = _summarize_chart_log(log) if log else {"chart_log": "unavailable"}
            chart_context = json.dumps(
                {
                    "active_chart_id": payload.active_chart_id,
                    "active_chart_name": payload.active_chart_name,
                    "chart_log": summary,
                    "chart_data": chart_data,
                },
                ensure_ascii=False,
            )
            chart_context_obj = AgentContext(
                settings=app.state.settings,
                conn=conn,
                chart_id=payload.active_chart_id,
                chart_name=payload.active_chart_name,
                chart_data=chart_data,
            )
            app.state.context_cleared = False
        else:
            chart_context_obj = AgentContext(
                settings=app.state.settings,
                conn=conn,
            )
            app.state.context_cleared = False

        async def event_generator() -> Any:
            final_answer = None
            async for event in agent_runner.respond_stream(
                payload.message,
                payload.history or [],
                context=chart_context,
                context_obj=chart_context_obj,
                debug=payload.debug,
            ):
                if event.get("event") == "final":
                    final_answer = event.get("answer")
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            if final_answer is None:
                final_answer = "No answer."

            chat_payload = DuckDBWriter.build_streamlit_chat_payload(
                session_id=payload.session_id,
                request_id=request_id,
                user_id=payload.user_id,
                message=payload.message,
                response=final_answer,
                latency_ms=0,
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
        latest = _fetch_latest_active_chart(conn)
        if not latest or not latest.get("slice_id"):
            return {"context": None}
        log = _fetch_latest_chart_log(conn, int(latest["slice_id"]))
        summary = _summarize_chart_log(log) if log else {"chart_log": "unavailable"}
        return {
            "context": {
                "chart_id": latest.get("slice_id"),
                "chart_name": None,
                "chart_log": summary,
                "chart_data": None,
            }
        }

    @app.get("/chat/debug/latest")
    def chat_debug_latest() -> dict[str, Any]:
        dbg = app.state.last_chat_debug
        if not dbg:
            return {"debug": None}
        return {"debug": dbg}

    @app.get("/chat/dialogue")
    def chat_dialogue(
        session_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be > 0")
        conn = app.state.conn
        rows = conn.execute(
            """
            SELECT ts, user_id, message, response
            FROM streamlit_chat_logs
            WHERE session_id = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
            [session_id, limit],
        ).fetchall()
        dialogue = []
        for ts, user_id, message, response in rows:
            if message:
                dialogue.append(
                    {
                        "role": "user",
                        "content": message,
                        "ts": ts.isoformat() if ts else None,
                        "user_id": user_id,
                    }
                )
            if response:
                dialogue.append(
                    {
                        "role": "assistant",
                        "content": response,
                        "ts": ts.isoformat() if ts else None,
                        "user_id": user_id,
                    }
                )
        return {"session_id": session_id, "dialogue": dialogue}

    @app.delete("/chat/dialogue")
    def clear_chat_dialogue(
        session_id: str,
    ) -> StatusResponse:
        if not session_id:
            raise HTTPException(status_code=400, detail="session_id is required")
        conn = app.state.conn
        conn.execute(
            "DELETE FROM streamlit_chat_logs WHERE session_id = ?",
            [session_id],
        )
        return StatusResponse(status="cleared")

    @app.delete("/chat/context")
    def clear_chat_context() -> StatusResponse:
        app.state.last_chat_context = None
        app.state.context_cleared = True
        return StatusResponse(status="cleared")

    @app.post("/events", response_model=StatusResponse)
    async def events(
        payload: EventRequest, writer: DuckDBWriter = Depends(get_writer)
    ) -> StatusResponse:
        event_id = time.time_ns()
        event_ts = payload.ts or datetime.now(timezone.utc)
        ui_payload = DuckDBWriter.build_ui_payload(
            event_id=event_id,
            ts=event_ts,
            session_id=payload.session_id,
            user_id=payload.user_id,
            event_type=payload.event_type,
            payload=payload.payload,
        )
        await writer.enqueue_ui_event(ui_payload)
        return StatusResponse(status="ok")

    return app


app = create_app()
