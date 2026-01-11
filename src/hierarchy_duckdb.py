from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import json
import duckdb
import pandas as pd
import numpy as np
from pathlib import Path
import datetime as dt
import yaml
from loguru import logger

from unique_index import UniqueIndex

# -----------------------------
# Helpers
# -----------------------------

def _is_orderable(dtype: str) -> bool:
    d = dtype.lower()
    if any(x in d for x in ["int", "decimal", "numeric", "double", "float", "real"]):
        return True
    if any(x in d for x in ["date", "timestamp", "time"]):
        return True
    return False

def _normalize_dtype(dtype: str) -> str:
    d = dtype.lower()
    if "int" in d:
        return "integer"
    if any(x in d for x in ["decimal", "numeric", "double", "float", "real"]):
        return "numeric"
    if "boolean" in d or d == "bool":
        return "boolean"
    if "date" in d and "time" in d:
        return "timestamp"
    if "timestamp" in d:
        return "timestamp"
    if "date" in d:
        return "date"
    return "string"

def _jsonable(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    try:
        json.dumps(v)
        return v
    except Exception:
        return str(v)


# -----------------------------
# DuckDB Introspection
# -----------------------------

def get_table_columns(conn: duckdb.DuckDBPyConnection, table: str) -> Dict[str, str]:
    """
    Get column names and types of a table in DuckDB.
    df columns: Index(['cid', 'name', 'type', 'notnull', 'dflt_value', 'pk'], dtype='object')
    """
    df = conn.execute(f"PRAGMA table_info({table})").fetchdf()
    return {row["name"]: row["type"] for _, row in df.iterrows()}

def compute_column_stats(
        conn: duckdb.DuckDBPyConnection, 
        table: str, 
        column: str, 
        dtype: str, 
        index_dir_path: str | Path
        ) -> Dict[str, Any]:
    """
    Compute basic stats and materialize unique_values either as:
      - a list (if distinct_count <= 30)
      - an external B+Tree index file (if distinct_count > 30) stored under ./data/index

    The unique_values field will be:
      - List[Any] when small
      - Dict metadata when large: {"type":"bplustree","path":"{index_dir_path}/<table>__<column>.bpt","count":<distinct_count>}
    """
    index_dir_path = str(index_dir_path)
    ident = f'"{column}"'
    stats = {
        "count": None,
        "null_count": None,
        "distinct_count": None,
        "min": None,
        "max": None,
        "range": None,
        "dtype": _normalize_dtype(dtype),
        "unique_values": None,
    }

    try:
        # Count, Null count
        q = f"SELECT COUNT(*)::BIGINT AS cnt, SUM(CASE WHEN {ident} IS NULL THEN 1 ELSE 0 END)::BIGINT AS nulls FROM {table}"
        cnt, nulls = conn.execute(q).fetchone()
        stats["count"] = int(cnt)
        stats["null_count"] = int(nulls)

        # Distinct count
        dq = f"SELECT COUNT(DISTINCT {ident})::BIGINT FROM {table}"
        distinct_count = int(conn.execute(dq).fetchone()[0])
        stats["distinct_count"] = distinct_count

        # If orderable type, get min/max
        if _is_orderable(dtype):
            mq = f"SELECT MIN({ident}), MAX({ident}) FROM {table} WHERE {ident} IS NOT NULL"
            minv, maxv = conn.execute(mq).fetchone()
            if minv is not None and maxv is not None:
                minv = _jsonable(minv)
                maxv = _jsonable(maxv)
            stats["min"] = minv
            stats["max"] = maxv
            stats["range"] = [minv, maxv] if (minv is not None and maxv is not None) else None

        # Unique values
        if distinct_count <= 30:
            uq = f"SELECT DISTINCT {ident} FROM {table} WHERE {ident} IS NOT NULL ORDER BY {ident}"
            values = [_jsonable(v) for (v,) in conn.execute(uq).fetchall()]
            stats["unique_values"] = {
                "type": "list",
                "values": values
            }
        else:
            index_path = f"{index_dir_path}/{table}__{column}"

            # Build index with DISTINCT values
            uq = f"SELECT DISTINCT {ident} FROM {table} WHERE {ident} IS NOT NULL"
            values = [v for (v,) in conn.execute(uq).fetchall()]
            idx = UniqueIndex(index_path)

            for v in values:
                idx.add(v)

            stats["unique_values"] = {
                "type": "bplustree",
                "values": index_path
            }

    except Exception as e:
        stats["error"] = str(e)

    return stats


# -----------------------------
# Hierarchy nodes
# -----------------------------

@dataclass
class Node:
    """Node representation in the hierarchy.
    - name: column name
    - node_type: type of the node (e.g., 'attribute', 'measure')
    - label: human-readable label
    - children: list of child nodes
    - stats: optional statistics
    """
    name: str
    node_type: str
    label: Optional[str] = None
    children: List["Node"] = field(default_factory=list)
    stats: Optional[Dict[str, Any]] = None
    agg: Optional[str] = None  # placeholder for measure type
    
    def to_dict(self) -> Dict[str, Any]:
        x = {
            "name": self.name,
            "node_type": self.node_type,
            "label": self.label,
            "stats": self.stats,
            "children": [c.to_dict() for c in self.children]
        }
        if self.agg is not None:
            x["agg"] = self.agg
        return x

@dataclass
class HierarchyTree:
    table: str
    table_type: str
    children: List[Node] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"table": self.table, "table_type": self.table_type, "children": [c.to_dict() for c in self.children]}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

# -----------------------------
# YAML -> Tree (schema only)
# -----------------------------

def _node_from_yaml(y: Dict[str, Any], is_fact: bool) -> Node:
    name = y.get("name")
    node_type = "measure" if is_fact else "attribute"
    label = y.get("label") if y.get("label") else name
    kids = [ _node_from_yaml(c, is_fact) for c in y.get("children", []) ]
    return Node(name=name, node_type=node_type, label=label, children=kids, agg=y.get("agg"))

def load_hierarchy_yaml(path: str | Path) -> HierarchyTree:
    y = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    table = y.get("table")
    table_type = y.get("table_type")
    kids = [ _node_from_yaml(c, is_fact=(table_type == "fact")) for c in y.get("children", []) ]
    return HierarchyTree(table=table, table_type=table_type, children=kids)

# -----------------------------
# Attach stats using DuckDB
# -----------------------------

def _attach_stats_recursive(
        conn: duckdb.DuckDBPyConnection, 
        table: str, 
        node: Node, 
        col_types: Dict[str, str],
        index_path: str | Path = "./data/index"
    ) -> None:
    if node.name in col_types:
        dtype = col_types[node.name]
        node.stats = compute_column_stats(conn, table, node.name, dtype, index_path)

    for child in node.children:
        _attach_stats_recursive(conn, table, child, col_types, index_path)

def build_tree_with_stats(
        yaml_path: str | Path, 
        index_path: str | Path, 
        duckdb_conn: duckdb.DuckDBPyConnection
    ) -> HierarchyTree:
    if not Path(index_path).exists():
        Path(index_path).mkdir(parents=True, exist_ok=True)
    tree = load_hierarchy_yaml(yaml_path)
    col_types = get_table_columns(duckdb_conn, tree.table)

    if tree.table_type != "fact":
        for child in tree.children:
            _attach_stats_recursive(duckdb_conn, tree.table, child, col_types, index_path)

    return tree

if __name__ == "__main__":
    from pathlib import Path
    import argparse

    # extract a json hierarchy from yaml file.
    # ./data/tpcds/database/tpcds.db"
    # ./data/tutorial/database/sales.db"
    # uv run hierarchy_duckdb.py --db_type "tutorial"
    parser = argparse.ArgumentParser(description="Generate hierarchy JSON files with stats from DuckDB.")
    parser.add_argument("--db_type", type=str, default="tpcds", help="Type of database to use")
    args = parser.parse_args()

    db_name = {
        "tpcds": "tpcds.db",
        "sales": "sales.db"
    }.get(args.db_type)
    if db_name is None:
        raise ValueError(f"Unsupported db_type: {args.db_type}. supported: tpcds, tutorial")

    proj_path = Path().resolve()
    assert proj_path.stem.lower() == 'agent4olap', f"Unexpected project path: {proj_path}"
    # proj_path = execution_path.parent

    # assert execution_path.stem == "", f"Unexpected project path: {execution_path}"

    data_path = proj_path / 'data' / args.db_type
    database_path = data_path / 'database' / db_name
    duckdb_conn = duckdb.connect(database=str(database_path))
    logger.info(f"Connected to DuckDB database at {database_path}")


    index_path = (data_path / 'index').relative_to(proj_path)
    logger.info(f"Using index path at {index_path}")
    for yaml_path in (data_path / 'hierarchy').glob('*.yaml'):
        tree = build_tree_with_stats(yaml_path, index_path, duckdb_conn)
        with (data_path / 'hierarchy' / f"{yaml_path.stem}.json").open('w') as f:
            f.write(tree.to_json())
        logger.info(f"Generated hierarchy JSON with stats at {(data_path / 'hierarchy' / f'{yaml_path.stem}.json')}")
