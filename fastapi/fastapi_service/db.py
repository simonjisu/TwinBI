from __future__ import annotations

import os
import re
from typing import Any
from pathlib import Path

import duckdb

CHECKPOINT_KEY_SUPERSET_LAST_ID = "superset_last_id"
_SAFE_IDENTIFIER_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def _ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)


def sanitize_identifier(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    safe = _SAFE_IDENTIFIER_PATTERN.sub("-", text).strip("-")
    return safe or None


def resolve_user_duckdb_path(base_path: str, user_key: str | None) -> str:
    safe_user = sanitize_identifier(user_key)
    if not safe_user:
        return base_path
    path = Path(base_path)
    stem = path.stem or "events"
    suffix = path.suffix or ".duckdb"
    return str(path.with_name(f"{stem}-{safe_user}{suffix}"))


def connect(path: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    _ensure_parent_dir(path)
    return duckdb.connect(path, read_only=read_only)


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
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
            latency_ms BIGINT,
            model_name VARCHAR,
            prompt_tokens BIGINT,
            completion_tokens BIGINT,
            total_tokens BIGINT,
            token_cost BIGINT,
            usd_cost DOUBLE
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
    try:
        conn.execute("ALTER TABLE streamlit_chat_logs ADD COLUMN model_name VARCHAR")
    except duckdb.CatalogException:
        pass
    try:
        conn.execute("ALTER TABLE streamlit_chat_logs ADD COLUMN prompt_tokens BIGINT")
    except duckdb.CatalogException:
        pass
    try:
        conn.execute("ALTER TABLE streamlit_chat_logs ADD COLUMN completion_tokens BIGINT")
    except duckdb.CatalogException:
        pass
    try:
        conn.execute("ALTER TABLE streamlit_chat_logs ADD COLUMN total_tokens BIGINT")
    except duckdb.CatalogException:
        pass
    try:
        conn.execute("ALTER TABLE streamlit_chat_logs ADD COLUMN token_cost BIGINT")
    except duckdb.CatalogException:
        pass
    try:
        conn.execute("ALTER TABLE streamlit_chat_logs ADD COLUMN usd_cost DOUBLE")
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


def get_next_superset_log_id(conn: duckdb.DuckDBPyConnection) -> int:
    return get_max_superset_log_id(conn) + 1


def insert_streamlit_chat_log(
    conn: duckdb.DuckDBPyConnection, payload: dict[str, Any]
) -> None:
    conn.execute(
        """
        INSERT INTO streamlit_chat_logs (
            ts, session_id, request_id, user_id, message, response, response_raw, response_events,
            latency_ms, model_name, prompt_tokens, completion_tokens, total_tokens, token_cost, usd_cost
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            payload.get("model_name"),
            payload.get("prompt_tokens"),
            payload.get("completion_tokens"),
            payload.get("total_tokens"),
            payload.get("token_cost"),
            payload.get("usd_cost"),
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
