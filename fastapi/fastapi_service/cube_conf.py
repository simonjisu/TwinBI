from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

JOIN_RE = re.compile(
    r"\{([^}]+)\}\.([A-Za-z0-9_]+)\s*=\s*\{([^}]+)\}\.([A-Za-z0-9_]+)"
)


def load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_join_keys(join_sql: str, cube_name: str) -> List[Dict[str, Any]]:
    keys: List[Dict[str, Any]] = []
    if not isinstance(join_sql, str):
        return keys

    m = JOIN_RE.search(join_sql)
    if not m:
        return keys

    left_tbl_raw, left_col, right_tbl_raw, right_col = m.groups()

    left_tbl = cube_name if left_tbl_raw == "CUBE" else left_tbl_raw
    right_tbl = cube_name if right_tbl_raw == "CUBE" else right_tbl_raw

    keys.append(
        {
            "from": f"{left_tbl}.{left_col}",
            "to": f"{right_tbl}.{right_col}",
            "from_table": left_tbl,
            "from_column": left_col,
            "to_table": right_tbl,
            "to_column": right_col,
        }
    )
    return keys


def build_schema_with_joins(
    sales_yml_path: str,
    view_yml_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    view_yml_paths = view_yml_paths or []

    sales_doc = load_yaml(sales_yml_path)
    cubes = sales_doc.get("cubes") or []

    cube_defs: Dict[str, Dict[str, Any]] = {}
    cube_join_map: Dict[str, Dict[str, Any]] = defaultdict(dict)

    for c in cubes:
        name = c.get("name")
        if not name:
            continue

        dims = [d.get("name") for d in (c.get("dimensions") or []) if d.get("name")]
        meas = [m.get("name") for m in (c.get("measures") or []) if m.get("name")]
        sql_table = c.get("sql_table")

        cube_defs[name] = {
            "type": "cube",
            "sql_table": sql_table,
            "dimensions": dims,
            "measures": meas,
        }

        for j in (c.get("joins") or []):
            jname = j.get("name")
            if not jname:
                continue
            rel = j.get("relationship")
            sql = j.get("sql")
            cube_join_map[name][jname] = {
                "relationship": rel,
                "sql": sql,
                "joined_key": parse_join_keys(sql, cube_name=name),
            }

    def short(member: str) -> str:
        return (
            member.split(".", 1)[-1]
            if isinstance(member, str) and "." in member
            else member
        )

    out: Dict[str, Any] = {}

    for cube_name, info in cube_defs.items():
        cols = set()
        for d in info["dimensions"]:
            cols.add(short(d))
        for m in info["measures"]:
            cols.add(short(m))

        rec: Dict[str, Any] = {
            "sql_table": info.get("sql_table"),
            "columns": sorted(cols),
        }
        if cube_join_map.get(cube_name):
            rec["joined"] = cube_join_map[cube_name]
        out[cube_name] = rec

    for view_path in view_yml_paths:
        doc = load_yaml(view_path)
        for v in (doc.get("views") or []):
            vname = v.get("name")
            if not vname:
                continue

            cols = []
            joins_for_view: Dict[str, Any] = {}

            for ref in (v.get("cubes") or []):
                join_path = ref.get("join_path")
                includes = ref.get("includes") or []
                prefix = bool(ref.get("prefix"))

                if not join_path:
                    continue

                parts = join_path.split(".")
                base = parts[0]
                target = parts[-1] if len(parts) > 1 else base

                for inc in includes:
                    if target != base and prefix:
                        cols.append(f"{target}_{inc}")
                    else:
                        cols.append(inc)

                if target != base:
                    jdef = cube_join_map.get(base, {}).get(target)
                    if jdef:
                        joins_for_view[target] = {"from": base, "to": target, **jdef}
                    else:
                        joins_for_view[target] = {"from": base, "to": target}

            out[vname] = {
                "type": "view",
                "description": v.get("description"),
                "columns": sorted(set(cols)),
                **({"joined": joins_for_view} if joins_for_view else {}),
            }

    return out


def load_repo_schema(conf_root: str) -> Dict[str, Any]:
    root = Path(conf_root)
    sales_path = root / "model" / "cubes" / "sales.yml"
    view_paths = list((root / "model" / "views").glob("*.yml"))
    return build_schema_with_joins(
        sales_yml_path=str(sales_path),
        view_yml_paths=[str(p) for p in view_paths],
    )
