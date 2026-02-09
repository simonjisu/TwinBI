from __future__ import annotations

import requests

from fastapi_service.config import Settings


def _require_setting(value: str | None, label: str) -> str:
    if not value:
        raise ValueError(f"{label} not configured")
    return value


def _get_base_url(settings: Settings) -> str:
    return _require_setting(
        settings.superset_internal_url or settings.superset_public_url,
        "SUPERSET_INTERNAL_URL or SUPERSET_PUBLIC_URL",
    )


def _api_session_with_bearer(settings: Settings) -> requests.Session:
    username = _require_setting(settings.superset_username, "SUPERSET_USERNAME")
    password = _require_setting(settings.superset_password, "SUPERSET_PASSWORD")
    base_url = _get_base_url(settings)

    session = requests.Session()
    response = session.post(
        f"{base_url}/api/v1/security/login",
        json={
            "username": username,
            "password": password,
            "provider": "db",
            "refresh": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    access_token = response.json().get("access_token")
    if not access_token:
        raise RuntimeError("Superset login missing access token")
    session.headers.update({"Authorization": f"Bearer {access_token}"})
    return session


def _ensure_csrf(session: requests.Session, base_url: str) -> None:
    response = session.get(f"{base_url}/api/v1/security/csrf_token/", timeout=30)
    response.raise_for_status()
    token = response.json().get("result")
    if not token:
        raise RuntimeError("Superset CSRF token missing")
    session.headers.update(
        {
            "X-CSRFToken": token,
            "X-CSRF-Token": token,
            "Referer": f"{base_url}/",
        }
    )
