from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import duckdb

from fastapi_service import db


class DuckDBWriter:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        await self._queue.put({"type": "stop"})
        if self._task:
            await self._task

    async def enqueue_ui_event(self, payload: dict[str, Any]) -> None:
        await self._queue.put({"type": "ui_event", "payload": payload})

    async def enqueue_streamlit_chat_log(self, payload: dict[str, Any]) -> None:
        await self._queue.put({"type": "streamlit_chat", "payload": payload})

    async def enqueue_superset_log(self, payload: dict[str, Any]) -> None:
        await self._queue.put({"type": "superset_log", "payload": payload})

    async def enqueue_checkpoint(self, key: str, value: str) -> None:
        await self._queue.put({"type": "checkpoint", "key": key, "value": value})

    def flush_blocking(self, timeout: float = 2.0) -> None:
        if not self._loop:
            return
        future = asyncio.run_coroutine_threadsafe(
            self._flush_async(), self._loop
        )
        future.result(timeout=timeout)

    async def _flush_async(self) -> None:
        event = asyncio.Event()
        await self._queue.put({"type": "flush", "event": event})
        await event.wait()

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            item_type = item.get("type")
            if item_type == "stop":
                break
            if item_type == "flush":
                item["event"].set()
                continue
            if item_type == "ui_event":
                db.insert_ui_event(self._conn, item["payload"])
                continue
            if item_type == "streamlit_chat":
                db.insert_streamlit_chat_log(self._conn, item["payload"])
                continue
            if item_type == "superset_log":
                db.insert_superset_action_log(self._conn, item["payload"])
                continue
            if item_type == "checkpoint":
                db.set_checkpoint(self._conn, item["key"], item["value"])
                continue

    def build_ui_payload(
        *,
        event_id: int,
        ts: datetime,
        session_id: str,
        user_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "ts": ts,
            "session_id": session_id,
            "user_id": user_id,
            "event_type": event_type,
            "payload_json": json.dumps(payload, ensure_ascii=True),
        }

    @staticmethod
    def build_streamlit_chat_payload(
        *,
        session_id: str,
        request_id: str,
        user_id: str | None,
        message: str,
        response: str,
        response_raw: str | None = None,
        response_events: list[dict[str, Any]] | None = None,
        latency_ms: int,
    ) -> dict[str, Any]:
        return {
            "ts": datetime.now(timezone.utc),
            "session_id": session_id,
            "request_id": request_id,
            "user_id": user_id,
            "message": message,
            "response": response,
            "response_raw": response_raw,
            "response_events": json.dumps(response_events, ensure_ascii=False)
            if response_events
            else None,
            "latency_ms": latency_ms,
        }

    @staticmethod
    def build_superset_payload(
        *,
        superset_log_id: int,
        dttm: datetime | None,
        action: str | None,
        user_id: int | None,
        dashboard_id: int | None,
        slice_id: int | None,
        duration_ms: int | None,
        referrer: str | None,
        json_payload: str | None,
        ingested_at: datetime,
    ) -> dict[str, Any]:
        return {
            "superset_log_id": superset_log_id,
            "dttm": dttm,
            "action": action,
            "user_id": user_id,
            "dashboard_id": dashboard_id,
            "slice_id": slice_id,
            "duration_ms": duration_ms,
            "referrer": referrer,
            "json": json_payload,
            "ingested_at": ingested_at,
        }
