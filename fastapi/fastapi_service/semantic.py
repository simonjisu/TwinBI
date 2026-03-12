from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import requests
import yaml

from fastapi_service import db
from fastapi_service.cube_conf import load_repo_schema, load_yaml
from fastapi_service.models import (
    CreateViewResult,
    SupersetDatasetSyncRequest,
    SupersetDatasetSyncResult,
    ViewSpec,
)
from fastapi_service.superset_client import (
    _api_session_with_bearer,
    _ensure_csrf,
    _get_base_url,
)
from fastapi_service.config import Settings


def fetch_cube_meta(settings: Settings) -> dict[str, Any]:
    base_url = settings.cube_rest_url
    if not base_url:
        raise ValueError("CUBE_REST_URL not configured")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/cubejs-api/v1"):
        url = f"{base_url}/meta"
    else:
        url = f"{base_url}/cubejs-api/v1/meta"
    headers = {}
    if settings.cube_api_token:
        headers["Authorization"] = settings.cube_api_token
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Cube meta response is not a JSON object")
    return payload


def run_cube_query(
    settings: Settings,
    query: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    base_url = settings.cube_rest_url
    if not base_url:
        raise ValueError("CUBE_REST_URL not configured")
    base_url = base_url.rstrip("/")
    if base_url.endswith("/cubejs-api/v1"):
        url = f"{base_url}/load"
    else:
        url = f"{base_url}/cubejs-api/v1/load"
    headers = {}
    if settings.cube_api_token:
        headers["Authorization"] = settings.cube_api_token
    payload = {"query": query}
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Cube query response is not a JSON object")
    return data


def summarize_schema(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Input: Cube meta JSON in the form {"cubes":[...]}
    Output: {table_name: {columns:[...], joined:{joined_table:{columns:[...]}}}}
    """
    cubes = meta.get("cubes", []) or []

    pk_map: Dict[str, Set[str]] = defaultdict(set)
    cube_names: Set[str] = set()

    for c in cubes:
        name = c.get("name")
        if not name:
            continue
        cube_names.add(name)
        for d in (c.get("dimensions") or []):
            if d.get("primaryKey"):
                pk_map[name].add(d["name"].split(".", 1)[-1])

    def member_short(member_name: str) -> str:
        return (
            member_name.split(".", 1)[-1]
            if isinstance(member_name, str) and "." in member_name
            else member_name
        )

    def alias_table_col(alias: str) -> tuple[str, str]:
        t, col = alias.split(".", 1)
        return t, col

    out: Dict[str, Any] = {}

    for c in cubes:
        table = c.get("name")
        if not table:
            continue

        cols: Set[str] = set()
        joined: Dict[str, Set[str]] = defaultdict(set)

        for m in (c.get("measures") or []):
            mname = m.get("name")
            if not mname:
                continue
            cols.add(member_short(mname))

            alias = m.get("aliasMember")
            if isinstance(alias, str) and "." in alias:
                jt, jcol = alias_table_col(alias)
                joined[jt].add(jcol)

        for d in (c.get("dimensions") or []):
            dname = d.get("name")
            if not dname:
                continue
            dshort = member_short(dname)
            cols.add(dshort)

            alias = d.get("aliasMember")
            if isinstance(alias, str) and "." in alias:
                jt, jcol = alias_table_col(alias)
                joined[jt].add(jcol)

        for col in list(cols):
            if col.endswith("_key"):
                for other_cube, pks in pk_map.items():
                    if other_cube != table and col in pks:
                        joined[other_cube].add(col)

        out[table] = {"columns": sorted(cols)}
        if joined:
            out[table]["joined"] = {
                jt: {"columns": sorted(list(jcols))} for jt, jcols in joined.items()
            }

    return out


def extract_join_map(meta: Dict[str, Any]) -> Dict[str, Dict[str, List[Tuple[str, str]]]]:
    """
    Output: {cube_name: {join_cube: [(left_col, right_col), ...]}}
    """
    cubes = meta.get("cubes", []) or []
    join_map: Dict[str, Dict[str, List[Tuple[str, str]]]] = {}

    for cube in cubes:
        cube_name = cube.get("name")
        if not cube_name:
            continue
        joins = cube.get("joins") or {}
        if not isinstance(joins, dict):
            continue
        cube_joins: Dict[str, List[Tuple[str, str]]] = {}
        for join_name, join_def in joins.items():
            if not isinstance(join_def, dict):
                continue
            sql = join_def.get("sql")
            if not isinstance(sql, str):
                continue
            pairs = _parse_join_sql(sql, cube_name, join_name)
            if pairs:
                cube_joins[join_name] = pairs
        if cube_joins:
            join_map[cube_name] = cube_joins

    return join_map


def _parse_join_sql(sql: str, left_cube: str, right_cube: str) -> List[Tuple[str, str]]:
    parts = [part.strip() for part in sql.split("AND")]
    pairs: List[Tuple[str, str]] = []
    for part in parts:
        if "=" not in part:
            continue
        left, right = [side.strip() for side in part.split("=", 1)]
        left = _replace_cube_alias(left, left_cube, right_cube)
        right = _replace_cube_alias(right, left_cube, right_cube)
        left_col = left.split(".", 1)[-1] if "." in left else left
        right_col = right.split(".", 1)[-1] if "." in right else right
        if left_col and right_col:
            pairs.append((left_col, right_col))
    return pairs


def _replace_cube_alias(expr: str, left_cube: str, right_cube: str) -> str:
    expr = expr.replace("{CUBE}", left_cube)
    expr = expr.replace(f"{{{right_cube}}}", right_cube)
    return expr


VIEW_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _normalize_join_path(base_cube: str, join_path: str) -> str:
    join_path = join_path.strip()
    if not join_path:
        return base_cube
    if "." in join_path:
        return join_path
    return f"{base_cube}.{join_path}"


def _normalize_view_spec(spec: ViewSpec) -> ViewSpec:
    if not isinstance(spec, ViewSpec):
        spec = ViewSpec.model_validate(spec)
    return spec


def _resolve_target_cube(join_path: str, base_cube: str) -> str:
    parts = join_path.split(".")
    if len(parts) >= 2:
        return parts[-1]
    return base_cube


def _validate_view_spec(spec: ViewSpec, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not VIEW_NAME_RE.match(spec.view_name):
        errors.append("view_name must be alphanumeric or underscore")
    base_entry = schema.get(spec.base_cube)
    if not base_entry:
        errors.append(f"base_cube '{spec.base_cube}' not found in schema")
        return errors

    base_columns = set(base_entry.get("columns") or [])
    for measure in spec.measures:
        if measure not in base_columns:
            errors.append(f"measure '{measure}' not in base_cube '{spec.base_cube}'")

    for dim in spec.dimensions:
        join_path = _normalize_join_path(spec.base_cube, dim.join_path)
        target_cube = _resolve_target_cube(join_path, spec.base_cube)
        target_entry = schema.get(target_cube)
        if not target_entry:
            errors.append(f"join target '{target_cube}' not found in schema")
            continue
        allowed = set(target_entry.get("columns") or [])
        for inc in dim.includes:
            if inc not in allowed:
                errors.append(
                    f"dimension '{inc}' not in join target '{target_cube}'"
                )
    return errors


def _build_view_yaml(spec: ViewSpec) -> dict[str, Any]:
    cubes: list[dict[str, Any]] = []
    if spec.measures:
        cubes.append(
            {
                "join_path": spec.base_cube,
                "includes": list(spec.measures),
            }
        )
    for dim in spec.dimensions:
        join_path = _normalize_join_path(spec.base_cube, dim.join_path)
        entry: dict[str, Any] = {
            "join_path": join_path,
            "includes": list(dim.includes),
        }
        if dim.prefix:
            entry["prefix"] = True
        cubes.append(entry)

    view: dict[str, Any] = {"name": spec.view_name, "cubes": cubes}
    if spec.description:
        view["description"] = spec.description
    return {"views": [view]}


def _find_existing_view(
    views_dir: Path, view_name: str
) -> tuple[Path, dict[str, Any], int] | None:
    for path in sorted(views_dir.glob("*.yml")):
        doc = load_yaml(str(path))
        views = doc.get("views")
        if not isinstance(views, list):
            continue
        for idx, view in enumerate(views):
            if isinstance(view, dict) and view.get("name") == view_name:
                return path, doc, idx
    return None


def _delete_view_file(conf_root: str, view_name: str) -> tuple[str, str]:
    root = Path(conf_root)
    views_dir = root / "model" / "views"
    if not views_dir.exists():
        raise FileNotFoundError("views directory not found")
    existing = _find_existing_view(views_dir, view_name)
    if not existing:
        raise FileNotFoundError(f"view '{view_name}' not found")
    path, doc, idx = existing
    views = doc.get("views") or []
    if isinstance(views, list):
        views.pop(idx)
        if views:
            doc["views"] = views
            yaml.safe_dump(
                doc,
                path.open("w", encoding="utf-8"),
                sort_keys=False,
                allow_unicode=True,
            )
        else:
            path.unlink(missing_ok=True)
    else:
        path.unlink(missing_ok=True)
    return "deleted", str(path)


def _write_view_file(
    conf_root: str, spec: ViewSpec
) -> tuple[str, str, list[str]]:
    root = Path(conf_root)
    views_dir = root / "model" / "views"
    views_dir.mkdir(parents=True, exist_ok=True)

    view_doc = _build_view_yaml(spec)
    view_entry = view_doc["views"][0]

    existing = _find_existing_view(views_dir, spec.view_name)
    warnings: list[str] = []

    if existing:
        path, doc, idx = existing
        views = doc.get("views") or []
        if isinstance(views, list):
            views[idx] = view_entry
            doc["views"] = views
        else:
            doc = view_doc
        status = "updated"
    else:
        path = views_dir / f"{spec.view_name}.yml"
        doc = view_doc
        status = "created"

    yaml.safe_dump(
        doc,
        path.open("w", encoding="utf-8"),
        sort_keys=False,
        allow_unicode=True,
    )
    return status, str(path), warnings


def _reload_cube_metadata(settings: Settings) -> str:
    if not settings.cube_rest_url:
        return "skipped"
    try:
        # Trigger a meta fetch to prompt Cube to reload schema if supported.
        fetch_cube_meta(settings)
        return "ok"
    except Exception:
        return "error"


def create_cube_view(
    settings: Settings,
    spec: ViewSpec,
) -> CreateViewResult:
    spec = _normalize_view_spec(spec)
    if not settings.cube_conf_path:
        raise ValueError("CUBE_CONF_PATH not configured")

    schema = load_repo_schema(settings.cube_conf_path)
    errors = _validate_view_spec(spec, schema)
    if errors:
        raise ValueError("; ".join(errors))

    status, view_file, warnings = _write_view_file(settings.cube_conf_path, spec)
    reload_status = _reload_cube_metadata(settings)

    return CreateViewResult(
        status=status,
        view_name=spec.view_name,
        view_file=view_file,
        cube_reload_status=reload_status,
        physical_name=spec.view_name,
        warnings=warnings,
    )


def delete_cube_view(
    settings: Settings,
    view_name: str,
) -> CreateViewResult:
    if not settings.cube_conf_path:
        raise ValueError("CUBE_CONF_PATH not configured")
    status, view_file = _delete_view_file(settings.cube_conf_path, view_name)
    reload_status = _reload_cube_metadata(settings)
    return CreateViewResult(
        status=status,
        view_name=view_name,
        view_file=view_file,
        cube_reload_status=reload_status,
        physical_name=view_name,
        warnings=[],
    )


def log_unified_event(conn: Any, action: str, payload: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    db.insert_superset_action_log(
        conn,
        {
            "superset_log_id": db.get_next_superset_log_id(conn),
            "dttm": now,
            "action": action,
            "user_id": None,
            "dashboard_id": payload.get("dashboard_id"),
            "slice_id": payload.get("slice_id"),
            "duration_ms": payload.get("duration_ms"),
            "referrer": payload.get("referrer"),
            "json": json.dumps(payload, ensure_ascii=False),
            "ingested_at": now,
        },
    )


def _extract_dataset_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, list):
            return result
    if isinstance(payload, list):
        return payload
    return []


def _find_dataset_by_table(
    session: Any,
    base_url: str,
    database_id: int,
    schema: str | None,
    table_name: str,
) -> dict[str, Any] | None:
    page = 0
    page_size = 100
    while True:
        response = session.get(
            f"{base_url}/api/v1/dataset/",
            params={"page": page, "page_size": page_size},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        datasets = _extract_dataset_list(payload)
        if not datasets:
            return None
        for ds in datasets:
            if not isinstance(ds, dict):
                continue
            db_value = ds.get("database")
            db_id = None
            if isinstance(db_value, dict):
                db_id = db_value.get("id")
            elif isinstance(db_value, int):
                db_id = db_value
            if db_id != database_id:
                continue
            ds_schema = ds.get("schema") or ds.get("db_schema")
            ds_table = ds.get("table_name") or ds.get("table") or ds.get("name")
            if ds_schema != schema:
                continue
            if ds_table == table_name:
                return ds
        if len(datasets) < page_size:
            return None
        page += 1


def _find_dataset_id_by_username(
    session: Any,
    base_url: str,
    username: str,
    database_id: int,
    schema: str | None,
    table_name: str,
    limit: int = 200,
) -> int | None:
    target_username = (username or "").strip().lower()
    if not target_username:
        return None
    page = 0
    page_size = 100
    remaining = max(1, limit)
    while remaining > 0:
        response = session.get(
            f"{base_url}/api/v1/dataset/",
            params={"page": page, "page_size": page_size},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        datasets = _extract_dataset_list(payload)
        if not datasets:
            return None
        for ds in datasets:
            if not isinstance(ds, dict):
                continue
            owners = ds.get("owners") or []
            owner_match = False
            for owner in owners:
                if not isinstance(owner, dict):
                    continue
                owner_username = str(owner.get("username") or "").strip().lower()
                if owner_username and owner_username == target_username:
                    owner_match = True
                    break
            if not owner_match:
                continue
            db_value = ds.get("database")
            db_id = None
            if isinstance(db_value, dict):
                db_id = db_value.get("id")
            elif isinstance(db_value, int):
                db_id = db_value
            if db_id != database_id:
                continue
            ds_schema = ds.get("schema") or ds.get("db_schema")
            ds_table = ds.get("table_name") or ds.get("table") or ds.get("name")
            if ds_schema != schema:
                continue
            if ds_table != table_name:
                continue
            value = ds.get("id") or ds.get("dataset_id")
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                continue
        if len(datasets) < page_size:
            return None
        page += 1
        remaining -= len(datasets)
    return None


def _refresh_dataset(
    session: Any, base_url: str, dataset_id: int
) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    endpoints = [
        f"{base_url}/api/v1/dataset/{dataset_id}/refresh",
        f"{base_url}/api/v1/dataset/{dataset_id}/refresh/",
    ]
    for url in endpoints:
        response = session.post(url, timeout=30)
        if response.status_code in {200, 201, 202, 204}:
            return True, warnings
        warnings.append(f"refresh failed {response.status_code}: {url}")
    return False, warnings


def _delete_dataset(session: Any, base_url: str, dataset_id: int) -> tuple[bool, str | None]:
    response = session.delete(
        f"{base_url}/api/v1/dataset/{dataset_id}",
        timeout=30,
    )
    if response.status_code in {200, 202, 204}:
        return True, None
    return False, response.text


def _wait_dataset_deleted(
    session: Any,
    base_url: str,
    database_id: int,
    schema: str | None,
    table_name: str,
    timeout_sec: int = 10,
    interval_sec: float = 0.5,
) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        existing = _find_dataset_by_table(
            session,
            base_url,
            database_id,
            schema,
            table_name,
        )
        if not existing:
            return True
        time.sleep(interval_sec)
    return False


def sync_superset_dataset(
    settings: Settings,
    request: SupersetDatasetSyncRequest,
) -> SupersetDatasetSyncResult:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    _ensure_csrf(session, base_url)

    dataset = _find_dataset_by_table(
        session,
        base_url,
        request.database_id,
        request.schema_name,
        request.table_name,
    )
    warnings: list[str] = []
    created = False
    updated = False
    dataset_id: int | None = None

    if not dataset:
        payload: dict[str, Any] = {
            "database": request.database_id,
            "schema": request.schema_name,
            "table_name": request.table_name,
        }
        if request.dataset_name:
            payload["dataset_name"] = request.dataset_name
        response = session.post(
            f"{base_url}/api/v1/dataset/",
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            if response.status_code == 422 and "already exists" in response.text:
                if request.force_refresh:
                    dataset = _find_dataset_by_table(
                        session,
                        base_url,
                        request.database_id,
                        request.schema_name,
                        request.table_name,
                    )
                    if not dataset:
                        raise RuntimeError(
                            f"Superset dataset create failed {response.status_code}: {response.text}"
                        )
                    existing_id = dataset.get("id") or dataset.get("dataset_id")
                    if existing_id is None:
                        raise RuntimeError(
                            f"Superset dataset create failed {response.status_code}: {response.text}"
                        )
                    deleted, delete_error = _delete_dataset(
                        session, base_url, int(existing_id)
                    )
                    if not deleted:
                        warnings.append(
                            "dataset refresh failed; delete fallback failed; manual refresh may be required"
                        )
                        if delete_error:
                            warnings.append(f"delete failed: {delete_error}")
                        updated = True
                        dataset_id = existing_id
                    else:
                        if not _wait_dataset_deleted(
                            session,
                            base_url,
                            request.database_id,
                            request.schema_name,
                            request.table_name,
                        ):
                            warnings.append(
                                "dataset delete did not finalize in time; recreate may fail"
                            )
                        response = session.post(
                            f"{base_url}/api/v1/dataset/",
                            json=payload,
                            timeout=30,
                        )
                        if response.status_code >= 400:
                            raise RuntimeError(
                                f"Superset dataset create failed {response.status_code}: {response.text}"
                            )
                        created = True
                        result = response.json()
                        if isinstance(result, dict):
                            ds = result.get("result") or result
                            if isinstance(ds, dict):
                                dataset_id = ds.get("id") or ds.get("dataset_id")
                else:
                    updated = True
                    warnings.append("dataset already exists; refresh skipped")
            else:
                raise RuntimeError(
                    f"Superset dataset create failed {response.status_code}: {response.text}"
                )
        else:
            created = True
            result = response.json()
            if isinstance(result, dict):
                ds = result.get("result") or result
                if isinstance(ds, dict):
                    dataset_id = ds.get("id") or ds.get("dataset_id")
    else:
        updated = True
        dataset_id = dataset.get("id") or dataset.get("dataset_id")

    def _recover_dataset_id() -> int | None:
        recovered = _find_dataset_by_table(
            session=session,
            base_url=base_url,
            database_id=request.database_id,
            schema=request.schema_name,
            table_name=request.table_name,
        )
        if isinstance(recovered, dict):
            value = recovered.get("id") or recovered.get("dataset_id")
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                pass
        by_owner = _find_dataset_id_by_username(
            session=session,
            base_url=base_url,
            username=settings.superset_username or "",
            database_id=request.database_id,
            schema=request.schema_name,
            table_name=request.table_name,
            limit=200,
        )
        return by_owner

    if dataset_id is None:
        recovered_id = _recover_dataset_id()
        if recovered_id is not None:
            dataset_id = recovered_id
            warnings.append("dataset id recovered via dataset lookup")
        else:
            warnings.append("dataset id not returned from Superset")
    else:
        if request.force_refresh or updated:
            ok, refresh_warnings = _refresh_dataset(session, base_url, int(dataset_id))
            warnings.extend(refresh_warnings)
            if not ok:
                # Fallback: delete + recreate when refresh endpoints are unsupported
                deleted, delete_error = _delete_dataset(
                    session, base_url, int(dataset_id)
                )
                if not deleted:
                    warnings.append(
                        "dataset refresh failed; delete fallback failed; manual refresh may be required"
                    )
                    if delete_error:
                        warnings.append(f"delete failed: {delete_error}")
                else:
                    recreate_payload: dict[str, Any] = {
                        "database": request.database_id,
                        "schema": request.schema_name,
                        "table_name": request.table_name,
                    }
                    if request.dataset_name:
                        recreate_payload["dataset_name"] = request.dataset_name
                    response = session.post(
                        f"{base_url}/api/v1/dataset/",
                        json=recreate_payload,
                        timeout=30,
                    )
                    if response.status_code >= 400:
                        warnings.append(
                            f"dataset recreate failed {response.status_code}: {response.text}"
                        )
                    else:
                        created = True
                        updated = False
                        result = response.json()
                        if isinstance(result, dict):
                            ds = result.get("result") or result
                            if isinstance(ds, dict):
                                dataset_id = ds.get("id") or ds.get("dataset_id")
                        if dataset_id is None:
                            recovered_id = _recover_dataset_id()
                            if recovered_id is not None:
                                dataset_id = recovered_id
                                warnings.append("dataset id recovered via dataset lookup")
                            else:
                                warnings.append("dataset recreate succeeded but id missing")

    if dataset_id is None:
        recovered_id = _recover_dataset_id()
        if recovered_id is not None:
            dataset_id = recovered_id
            warnings.append("dataset id recovered via final lookup")

    status = "created" if created else "updated"
    return SupersetDatasetSyncResult(
        status=status,
        dataset_id=int(dataset_id) if dataset_id is not None else None,
        created=created,
        updated=updated,
        warnings=warnings,
    )


def log_create_view(
    conn: Any,
    view_spec: ViewSpec,
    result: CreateViewResult,
) -> None:
    log_unified_event(
        conn,
        "semantic_view_create",
        {
            "view_name": view_spec.view_name,
            "base_cube": view_spec.base_cube,
            "measures": view_spec.measures,
            "dimensions": [dim.model_dump() for dim in view_spec.dimensions],
            "result": result.model_dump(),
        },
    )


def log_dataset_sync(
    conn: Any,
    request: SupersetDatasetSyncRequest,
    result: SupersetDatasetSyncResult,
) -> None:
    log_unified_event(
        conn,
        "superset_dataset_sync",
        {
            "database_id": request.database_id,
            "schema": request.schema_name,
            "table_name": request.table_name,
            "dataset_name": request.dataset_name,
            "force_refresh": request.force_refresh,
            "result": result.model_dump(),
        },
    )
