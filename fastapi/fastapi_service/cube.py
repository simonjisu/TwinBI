from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Set, Tuple

import requests

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


def summarize_schema(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Input: {"cubes":[...]} 형태의 Cube meta JSON
    Output: {table_name: {columns:[...], joined:{joined_table:{columns:[...]}}}}
    """
    cubes = meta.get("cubes", []) or []

    # cubeName -> set(primaryKey column short name)
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
