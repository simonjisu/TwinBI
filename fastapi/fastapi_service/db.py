from __future__ import annotations

import os
from typing import Any

import duckdb

CHECKPOINT_KEY_SUPERSET_LAST_ID = "superset_last_id"


def _ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def connect(path: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    _ensure_parent_dir(path)
    return duckdb.connect(path, read_only=read_only)


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ui_events (
            event_id BIGINT,
            ts TIMESTAMP,
            session_id VARCHAR,
            user_id VARCHAR,
            event_type VARCHAR,
            payload_json VARCHAR
        )
        """
    )
    try:
        conn.execute("ALTER TABLE ui_events ADD COLUMN event_id BIGINT")
    except duckdb.CatalogException:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS streamlit_chat_logs (
            ts TIMESTAMP,
            session_id VARCHAR,
            request_id VARCHAR,
            user_id VARCHAR,
            message VARCHAR,
            response VARCHAR,
            response_raw VARCHAR,
            response_events VARCHAR,
            latency_ms BIGINT
        )
        """
    )
    try:
        conn.execute("ALTER TABLE streamlit_chat_logs ADD COLUMN response_raw VARCHAR")
    except duckdb.CatalogException:
        pass
    try:
        conn.execute("ALTER TABLE streamlit_chat_logs ADD COLUMN response_events VARCHAR")
    except duckdb.CatalogException:
        pass
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS superset_action_logs (
            superset_log_id BIGINT,
            dttm TIMESTAMP,
            action VARCHAR,
            user_id BIGINT,
            dashboard_id BIGINT,
            slice_id BIGINT,
            duration_ms BIGINT,
            referrer VARCHAR,
            json VARCHAR,
            ingested_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _checkpoint (
            key VARCHAR PRIMARY KEY,
            value VARCHAR
        )
        """
    )


def get_checkpoint(conn: duckdb.DuckDBPyConnection, key: str) -> str | None:
    result = conn.execute(
        "SELECT value FROM _checkpoint WHERE key = ?", [key]
    ).fetchone()
    if not result:
        return None
    return result[0]


def set_checkpoint(
    conn: duckdb.DuckDBPyConnection, key: str, value: str
) -> None:
    conn.execute(
        "INSERT INTO _checkpoint (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        [key, value],
    )


def delete_checkpoint(conn: duckdb.DuckDBPyConnection, key: str) -> None:
    conn.execute("DELETE FROM _checkpoint WHERE key = ?", [key])


def get_max_superset_log_id(conn: duckdb.DuckDBPyConnection) -> int:
    result = conn.execute(
        "SELECT COALESCE(MAX(superset_log_id), 0) FROM superset_action_logs"
    ).fetchone()
    return int(result[0]) if result else 0


def insert_ui_event(conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO ui_events (
            event_id, ts, session_id, user_id, event_type, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            payload.get("event_id"),
            payload["ts"],
            payload.get("session_id"),
            payload.get("user_id"),
            payload.get("event_type"),
            payload.get("payload_json"),
        ],
    )


def insert_streamlit_chat_log(
    conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]
) -> None:
    conn.execute(
        """
        INSERT INTO streamlit_chat_logs (
            ts, session_id, request_id, user_id, message, response, response_raw, response_events, latency_ms
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            payload["ts"],
            payload.get("session_id"),
            payload.get("request_id"),
            payload.get("user_id"),
            payload.get("message"),
            payload.get("response"),
            payload.get("response_raw"),
            payload.get("response_events"),
            payload.get("latency_ms"),
        ],
    )


def insert_superset_action_log(
    conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]
) -> None:
    conn.execute(
        """
        INSERT INTO superset_action_logs (
            superset_log_id, dttm, action, user_id, dashboard_id, slice_id,
            duration_ms, referrer, json, ingested_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            payload.get("superset_log_id"),
            payload.get("dttm"),
            payload.get("action"),
            payload.get("user_id"),
            payload.get("dashboard_id"),
            payload.get("slice_id"),
            payload.get("duration_ms"),
            payload.get("referrer"),
            payload.get("json"),
            payload.get("ingested_at"),
        ],
    )
