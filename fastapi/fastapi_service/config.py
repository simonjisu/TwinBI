from __future__ import annotations

from dataclasses import dataclass
import os


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _merge_origins(*values: list[str]) -> list[str]:
    merged: list[str] = []
    for origins in values:
        for origin in origins:
            if origin not in merged:
                merged.append(origin)
    return merged


@dataclass(frozen=True)
class Settings:
    duckdb_path: str
    superset_meta_db_uri: str | None
    superset_poll_interval_sec: float
    superset_batch_size: int
    superset_public_url: str | None
    superset_internal_url: str | None
    superset_username: str | None
    superset_password: str | None
    superset_guest_aud: str | None
    superset_log_dashboard_id: int | None
    superset_log_user_id: int | None
    superset_log_username: str | None
    cube_rest_url: str | None
    cube_api_token: str | None
    cube_sql_host: str | None
    cube_sql_port: int | None
    cube_conf_path: str | None
    cors_origins: list[str]


def load_settings() -> Settings:
    poll_interval = float(os.getenv("SUPERSET_POLL_INTERVAL_SEC", "2"))
    batch_size = int(os.getenv("SUPERSET_BATCH_SIZE", "200"))
    cube_sql_port = os.getenv("CUBE_SQL_PORT")
    superset_log_dashboard_id = os.getenv("SUPERSET_LOG_DASHBOARD_ID")
    superset_log_user_id = os.getenv("SUPERSET_LOG_USER_ID")
    return Settings(
        duckdb_path=os.getenv("DUCKDB_PATH"),
        superset_meta_db_uri=os.getenv("SUPERSET_META_DB_URI"),
        superset_poll_interval_sec=poll_interval,
        superset_batch_size=batch_size,
        superset_public_url=os.getenv("SUPERSET_PUBLIC_URL"),
        superset_internal_url=os.getenv("SUPERSET_INTERNAL_URL"),
        superset_username=os.getenv("SUPERSET_USERNAME"),
        superset_password=os.getenv("SUPERSET_PASSWORD"),
        superset_guest_aud=os.getenv("SUPERSET_GUEST_AUD"),
        superset_log_dashboard_id=(
            int(superset_log_dashboard_id)
            if superset_log_dashboard_id
            else None
        ),
        superset_log_user_id=(
            int(superset_log_user_id) if superset_log_user_id else None
        ),
        superset_log_username=os.getenv("SUPERSET_LOG_USERNAME"),
        cube_rest_url=os.getenv("CUBE_REST_URL"),
        cube_api_token=os.getenv("CUBE_API_TOKEN"),
        cube_sql_host=os.getenv("CUBE_SQL_HOST"),
        cube_sql_port=int(cube_sql_port) if cube_sql_port else None,
        cube_conf_path=os.getenv("CUBE_CONF_PATH"),
        cors_origins=_merge_origins(
            _split_csv(os.getenv("CORS_ORIGINS")),
            _split_csv(os.getenv("CORS_ORIGINS_APPEND")),
        ),
    )
