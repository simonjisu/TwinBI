from __future__ import annotations

from typing import Any

import requests


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def post_chat(
    *,
    base_url: str,
    session_id: str,
    user_id: str | None,
    message: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    payload = {
        "session_id": session_id,
        "user_id": user_id,
        "message": message,
    }
    url = _join_url(base_url, "/chat")
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def post_event(
    *,
    base_url: str,
    session_id: str,
    user_id: str | None,
    event_type: str,
    payload: dict[str, Any],
    timeout: float = 10.0,
) -> dict[str, Any]:
    body = {
        "session_id": session_id,
        "user_id": user_id,
        "event_type": event_type,
        "payload": payload,
    }
    url = _join_url(base_url, "/events")
    response = requests.post(url, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()
