from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import duckdb

from fastapi_service import db

logger = logging.getLogger(__name__)


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            try:
                return int(text)
            except ValueError:
                return None
    return None


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

    async def flush(self) -> None:
        await self._flush_async()

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
            try:
                if item_type == "stop":
                    break
                if item_type == "flush":
                    item["event"].set()
                    continue
                if item_type == "ui_event":
                    payload = item["payload"]
                    superset_log = self.build_superset_ui_payload(payload)
                    db.insert_superset_action_log(self._conn, superset_log)
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
            except Exception:
                logger.exception("DuckDBWriter failed to process queue item: %s", item_type)
                continue

    def build_ui_payload(
        *,
        event_id: int,
        ts: datetime,
        session_id: str,
        user_id: str | None,
        device_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "ts": ts,
            "session_id": session_id,
            "user_id": user_id,
            "device_id": device_id,
            "event_type": event_type,
            "payload_json": json.dumps(payload, ensure_ascii=True),
        }

    def build_superset_ui_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_payload = payload.get("payload_json")
        try:
            parsed_payload = json.loads(event_payload) if event_payload else {}
        except json.JSONDecodeError:
            parsed_payload = {}

        user_id = payload.get("user_id")
        numeric_user_id = None
        if isinstance(user_id, int):
            numeric_user_id = user_id
        elif isinstance(user_id, str) and user_id.isdigit():
            numeric_user_id = int(user_id)

        dashboard_id = _coerce_int(
            parsed_payload.get("dashboard_id") or parsed_payload.get("dashboardId")
        )
        slice_id = (
            _coerce_int(parsed_payload.get("slice_id"))
            or _coerce_int(parsed_payload.get("sliceId"))
            or _coerce_int(parsed_payload.get("chart_id"))
            or _coerce_int(parsed_payload.get("chartId"))
        )

        json_payload = json.dumps(
            {
                "source": "ui",
                "session_id": payload.get("session_id"),
                "user_id": user_id,
                "device_id": payload.get("device_id"),
                "event_type": payload.get("event_type"),
                "payload": parsed_payload,
            },
            ensure_ascii=False,
        )

        return {
            "superset_log_id": db.get_next_superset_log_id(self._conn),
            "dttm": payload.get("ts"),
            "action": payload.get("event_type"),
            "user_id": numeric_user_id,
            "dashboard_id": dashboard_id,
            "slice_id": slice_id,
            "duration_ms": None,
            "referrer": parsed_payload.get("referrer"),
            "json": json_payload,
            "ingested_at": datetime.now(timezone.utc),
        }

    @staticmethod
    def build_streamlit_chat_payload(
        *,
        session_id: str,
        request_id: str,
        user_id: str | None,
        device_id: str | None,
        message: str,
        response: str,
        response_raw: str | None = None,
        response_events: list[dict[str, Any]] | None = None,
        latency_ms: int,
        model_name: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        token_cost: int | None = None,
        usd_cost: float | None = None,
    ) -> dict[str, Any]:
        safe_message = message or ""
        safe_response = response or ""
        safe_response_raw = response_raw if response_raw is not None else safe_response
        safe_response_events = (
            response_events if isinstance(response_events, list) else []
        )
        return {
            "ts": datetime.now(timezone.utc),
            "session_id": session_id,
            "request_id": request_id,
            "user_id": user_id,
            "device_id": device_id,
            "message": safe_message,
            "response": safe_response,
            "response_raw": safe_response_raw,
            "response_events": json.dumps(safe_response_events, ensure_ascii=False),
            "latency_ms": latency_ms,
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "token_cost": token_cost,
            "usd_cost": usd_cost,
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
