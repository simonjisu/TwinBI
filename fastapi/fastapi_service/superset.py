from __future__ import annotations

import asyncio
import os
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import duckdb
import requests
import sqlglot

from fastapi_service import db
from fastapi_service.config import Settings
from fastapi_service.semantic import extract_join_map, fetch_cube_meta, summarize_schema
from fastapi_service.superset_client import (
    _api_session_with_bearer,
    _ensure_csrf,
    _get_base_url,
)
from fastapi_service.cube_conf import load_repo_schema
from fastapi_service.writer import DuckDBWriter

logger = logging.getLogger(__name__)

def make_adhoc_metric(
    column_name: str,
    aggregate: str,
    label: str | None = None,
) -> dict[str, Any]:
    """
    Superset /api/v1/chart/data adhoc metric generator.
    """
    agg = aggregate.upper().strip()
    if not label:
        label = f"{agg}({column_name})"
    return {
        "expressionType": "SIMPLE",
        "aggregate": agg,
        "column": {"column_name": column_name},
        "label": label,
    }


def _normalize_metrics(metrics: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(metrics, list):
        return normalized
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        aggregate = metric.get("aggregate")
        column = metric.get("column")
        column_name = column.get("column_name") if isinstance(column, dict) else None
        if aggregate and column_name:
            normalized.append(
                make_adhoc_metric(
                    column_name,
                    str(aggregate),
                    label=metric.get("label"),
                )
            )
        elif metric.get("expressionType") == "SIMPLE":
            normalized.append(metric)
    return normalized


def _normalize_query_metrics(query: dict[str, Any]) -> None:
    metrics = query.get("metrics")
    normalized = _normalize_metrics(metrics)
    if normalized:
        query["metrics"] = normalized


def _render_orderby_expr(expr: Any) -> str | None:
    if isinstance(expr, str):
        return expr
    if isinstance(expr, dict):
        label = expr.get("label")
        if isinstance(label, str) and label:
            return label
        col = expr.get("column")
        if isinstance(col, dict):
            col_name = col.get("column_name")
            if isinstance(col_name, str) and col_name:
                return col_name
        sql_expr = expr.get("sqlExpression")
        if isinstance(sql_expr, str) and sql_expr:
            return sql_expr
    return None


def _normalize_orderby(orderby: Any) -> list[list[Any]]:
    if not isinstance(orderby, list):
        return []
    normalized: list[list[Any]] = []
    for item in orderby:
        if not isinstance(item, (list, tuple)) or not item:
            continue
        expr = _render_orderby_expr(item[0])
        if not expr:
            continue
        direction = bool(item[1]) if len(item) > 1 else False
        normalized.append([expr, direction])
    return normalized


def normalize_chart_queries_result(result: dict[str, Any]) -> dict[str, Any]:
    output = dict(result)
    drop_query_keys = {
        "annotation_layers",
        "url_params",
        "custom_params",
        "custom_form_data",
    }
    drop_form_data_keys = {
        "truncate_metric",
        "show_empty_columns",
        "comparison_type",
        "annotation_layers",
        "forecastPeriods",
        "forecastInterval",
        "orientation",
        "x_axis_title_margin",
        "y_axis_title_margin",
        "y_axis_title_position",
        "sort_series_type",
        "color_scheme",
        "time_shift_color",
        "only_total",
        "show_legend",
        "legendType",
        "legendOrientation",
        "x_axis_time_format",
        "xAxisLabelInterval",
        "y_axis_format",
        "y_axis_bounds",
        "truncateXAxis",
        "rich_tooltip",
        "showTooltipTotal",
        "tooltipTimeFormat",
        "extra_form_data",
    }
    form_data = output.get("form_data")
    if isinstance(form_data, dict):
        normalized_form = dict(form_data)
        normalized_metrics = _normalize_metrics(normalized_form.get("metrics"))
        if normalized_metrics:
            normalized_form["metrics"] = normalized_metrics
        for key in drop_form_data_keys:
            normalized_form.pop(key, None)
        output["form_data"] = normalized_form
    queries = output.get("queries")
    if isinstance(queries, list):
        normalized_queries: list[Any] = []
        for query in queries:
            if isinstance(query, dict):
                normalized_query = dict(query)
                _normalize_query_metrics(normalized_query)
                if "orderby" in normalized_query:
                    normalized_query["orderby"] = _normalize_orderby(
                        normalized_query.get("orderby")
                    )
                for key in drop_query_keys:
                    normalized_query.pop(key, None)
                normalized_queries.append(normalized_query)
            else:
                normalized_queries.append(query)
        output["queries"] = normalized_queries
    return output


class QueryTranslater:
    SUPPORTED_ACTIONS = {
        "ChartDataRestApi.data",
        "ChartDataRestApi.json_dumps",
    }

    def __init__(
        self,
        settings: Settings | None = None,
        dataset_resolver: Callable[[int], dict[str, Any] | None] | None = None,
    ) -> None:
        self._settings = settings
        self._dataset_resolver = dataset_resolver
        self._dataset_cache: dict[int, dict[str, Any] | None] = {}
        self._cube_meta_cache: dict[str, Any] | None = None
        self._cube_schema_cache: dict[str, Any] | None = None
        self._cube_join_cache: dict[str, dict[str, list[tuple[str, str]]]] | None = None
        self._cube_conf_cache: dict[str, Any] | None = None

    def parse_payload(self, payload_json: str | None) -> dict[str, Any] | None:
        if not payload_json:
            return None
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return None

        query = None
        queries = payload.get("queries") if isinstance(payload, dict) else None
        if isinstance(queries, list) and queries:
            query = queries[0]
        elif isinstance(payload, dict):
            query = payload.get("form_data") or {}

        if not isinstance(query, dict):
            return None

        datasource = None
        if isinstance(payload, dict):
            datasource = payload.get("datasource") or query.get("datasource")
        columns = query.get("columns") or []
        metrics = query.get("metrics") or []
        dataset_id, _dtype = self._parse_datasource_id(datasource)
        dataset_schema = self._resolve_dataset_schema(dataset_id)
        table_name = self._render_datasource(datasource, dataset_schema)
        dataset_table = dataset_schema.get("table_name") if dataset_schema else None
        dataset_columns = (
            set(dataset_schema.get("columns") or []) if dataset_schema else set()
        )
        if not dataset_schema:
            cube_table = self._infer_table_from_cube(columns)
            if cube_table and not dataset_table:
                dataset_table = cube_table

        conf_entry = self._resolve_conf_entry(table_name)
        base_alias, join_tables, from_clause = self._render_from_clause_from_entry(
            table_name, conf_entry
        )
        if conf_entry:
            dataset_columns = set(conf_entry.get("columns") or [])

        select_exprs = self._render_select(
            columns, metrics, base_alias, join_tables, dataset_columns
        )
        form_data = payload.get("form_data") if isinstance(payload, dict) else {}
        filters = self._collect_filters(query, form_data)
        filters = self._dedupe_filters(filters)
        where_exprs = self._render_filters(
            filters, base_alias, join_tables, dataset_columns
        )
        extras = query.get("extras") or {}
        if isinstance(extras, dict):
            extra_where = extras.get("where")
            if extra_where:
                normalized_extra = self._qualify_extra_where(
                    str(extra_where), base_alias, join_tables
                )
                where_exprs.extend(self._split_extra_where(normalized_extra))
        group_by = self._render_group_by(columns, base_alias, join_tables, dataset_columns)
        order_by = self._render_order_by(
            query.get("orderby") or [], base_alias, join_tables, dataset_columns
        )
        limit = query.get("row_limit")

        select_clause = ", ".join(select_exprs) if select_exprs else "*"
        sql = f"SELECT {select_clause}"
        sql += f" {from_clause}" if from_clause else " FROM datasource"
        if where_exprs:
            sql += " WHERE " + " AND ".join(where_exprs)
        if group_by:
            sql += " GROUP BY " + ", ".join(group_by)
        if order_by:
            sql += " ORDER BY " + ", ".join(order_by)
        if isinstance(limit, int) and limit > 0:
            sql += f" LIMIT {limit}"
        sql = self._pretty_sql(sql)
        return {
            "sql": sql,
            "filters": self._normalize_filters(filters),
            "where": where_exprs,
        }

    def translate_payload(self, payload_json: str | None) -> str | None:
        parsed = self.parse_payload(payload_json)
        if not parsed:
            return None
        return parsed.get("sql")

    @staticmethod
    def _split_extra_where(where_text: str) -> list[str]:
        text = where_text.strip()
        if not text:
            return []
        if text.startswith("(") and text.endswith(")"):
            inner = text[1:-1].strip()
            if QueryTranslater._has_balanced_parens(inner):
                text = inner
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_single = False
        in_double = False
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == "(":
                    depth += 1
                elif ch == ")" and depth > 0:
                    depth -= 1
            if (
                not in_single
                and not in_double
                and depth == 0
                and text[i : i + 5] == " AND "
            ):
                part = "".join(buf).strip()
                if part:
                    parts.append(part)
                buf = []
                i += 5
                continue
            buf.append(ch)
            i += 1
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
        return parts or [text]

    @staticmethod
    def _qualify_extra_where(
        where_text: str,
        base_alias: str | None,
        join_tables: set[str],
    ) -> str:
        text = where_text.strip()
        if not text:
            return text
        tables = list(join_tables)
        if base_alias:
            tables.append(base_alias)

        def rewrite_segment(segment: str) -> str:
            for table in tables:
                pattern = rf"\\b{re.escape(table)}_([A-Za-z0-9_]+)\\b"
                segment = re.sub(pattern, rf"{table}.\\1", segment)
            return segment

        out: list[str] = []
        buf: list[str] = []
        in_single = False
        in_double = False

        def flush_buf() -> None:
            if buf:
                out.append(rewrite_segment("".join(buf)))
                buf.clear()

        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "'" and not in_double:
                flush_buf()
                in_single = not in_single
                out.append(ch)
                i += 1
                continue
            if ch == '"' and not in_single:
                flush_buf()
                in_double = not in_double
                out.append(ch)
                i += 1
                continue
            if in_single or in_double:
                out.append(ch)
            else:
                buf.append(ch)
            i += 1

        flush_buf()
        return "".join(out)

    @staticmethod
    def _has_balanced_parens(text: str) -> bool:
        depth = 0
        in_single = False
        in_double = False
        for ch in text:
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        return False
                    depth -= 1
        return depth == 0

    def _pretty_sql(self, sql: str) -> str:
        try:
            return sqlglot.transpile(sql, read="duckdb", pretty=True)[0]
        except Exception:
            return sql

    def _parse_datasource_id(self, datasource: Any) -> tuple[int | None, str | None]:
        if isinstance(datasource, dict):
            did = datasource.get("id")
            dtype = datasource.get("type")
            return (int(did), dtype) if did is not None else (None, dtype)
        if isinstance(datasource, str) and "__" in datasource:
            left, right = datasource.split("__", 1)
            try:
                return int(left), right
            except ValueError:
                return None, right
        return None, None

    def _render_datasource(
        self, datasource: Any, dataset_schema: dict[str, Any] | None
    ) -> str | None:
        if dataset_schema:
            table_name = dataset_schema.get("table_name")
            schema = dataset_schema.get("schema")
            if table_name:
                return f"{schema}.{table_name}" if schema else table_name
        if isinstance(datasource, dict):
            dtype = datasource.get("type")
            did = datasource.get("id")
            if dtype and did:
                return f"{dtype}_{did}"
        if isinstance(datasource, str):
            return datasource.replace("__", "_")
        return None

    def _render_select(
        self,
        columns: list[Any],
        metrics: list[Any],
        base_alias: str | None,
        join_tables: set[str],
        dataset_columns: set[str],
    ) -> list[str]:
        selects: list[str] = []
        for col in columns:
            col_name = self._render_column(
                col, base_alias, join_tables, dataset_columns
            )
            if col_name:
                selects.append(col_name)
        for metric in metrics:
            metric_expr = self._render_metric(
                metric, base_alias, join_tables, dataset_columns
            )
            if metric_expr:
                selects.append(metric_expr)
        return selects

    def _render_group_by(
        self,
        columns: list[Any],
        base_alias: str | None,
        join_tables: set[str],
        dataset_columns: set[str],
    ) -> list[str]:
        group_by: list[str] = []
        for col in columns:
            col_name = self._render_column(
                col, base_alias, join_tables, dataset_columns
            )
            if col_name:
                group_by.append(col_name)
        return group_by

    def _render_order_by(
        self,
        orderby: list[Any],
        base_alias: str | None,
        join_tables: set[str],
        dataset_columns: set[str],
    ) -> list[str]:
        rendered: list[str] = []
        for item in orderby:
            if not isinstance(item, (list, tuple)) or not item:
                continue
            expr = self._render_metric(
                item[0], base_alias, join_tables, dataset_columns
            ) or self._render_column(item[0], base_alias, join_tables, dataset_columns)
            if not expr:
                continue
            desc = bool(len(item) > 1 and item[1])
            rendered.append(f"{expr} {'DESC' if desc else 'ASC'}")
        return rendered

    def _render_column(
        self,
        col: Any,
        base_alias: str | None,
        join_tables: set[str],
        dataset_columns: set[str],
    ) -> str | None:
        if isinstance(col, str):
            return self._qualify_column(
                col, base_alias, join_tables, dataset_columns
            )
        if isinstance(col, dict):
            name = col.get("column_name") or col.get("label")
            return self._qualify_column(
                name, base_alias, join_tables, dataset_columns
            )
        return None

    def _render_metric(
        self,
        metric: Any,
        base_alias: str | None,
        join_tables: set[str],
        dataset_columns: set[str],
    ) -> str | None:
        if isinstance(metric, str):
            return metric
        if not isinstance(metric, dict):
            return None
        if metric.get("sqlExpression"):
            return metric["sqlExpression"]
        aggregate = metric.get("aggregate")
        column = metric.get("column") or {}
        column_name = (
            column.get("column_name") if isinstance(column, dict) else None
        )
        if aggregate and column_name:
            column_name = self._qualify_column(
                column_name, base_alias, join_tables, dataset_columns
            )
            return f"{aggregate}({column_name})"
        return metric.get("label")

    def _render_filters(
        self,
        filters: list[Any],
        base_alias: str | None,
        join_tables: set[str],
        dataset_columns: set[str],
    ) -> list[str]:
        rendered: list[str] = []
        for flt in filters:
            if not isinstance(flt, dict):
                continue
            raw_col, op, val = self._coerce_filter_fields(flt)
            col_name = self._extract_filter_col_name(raw_col)
            if not col_name or not op:
                continue
            col = self._qualify_column(
                col_name, base_alias, join_tables, dataset_columns
            )
            if not col:
                continue
            val = self._unwrap_filter_value(val)
            if op == "TEMPORAL_RANGE":
                if not val or val == "No filter":
                    continue
                rendered.append(f"{col} BETWEEN {self._render_value(val)}")
                continue
            if op in {"IN", "NOT IN"}:
                rendered.append(f"{col} {op} {self._render_value(val)}")
                continue
            if op in {"==", "=", "!=", "<>", ">", ">=", "<", "<="}:
                rendered.append(f"{col} {op} {self._render_value(val)}")
                continue
        return rendered

    def _render_value(self, val: Any) -> str:
        if isinstance(val, list):
            rendered = ", ".join(self._render_value(v) for v in val)
            return f"({rendered})"
        if isinstance(val, dict):
            nested = self._unwrap_filter_value(val)
            if nested is not None and nested is not val:
                return self._render_value(nested)
        if isinstance(val, (int, float)):
            return str(val)
        if val is None:
            return "NULL"
        return f"'{str(val)}'"

    def _qualify_column(
        self,
        col_name: str | None,
        base_alias: str | None,
        join_tables: set[str],
        dataset_columns: set[str],
    ) -> str | None:
        if not col_name or not isinstance(col_name, str):
            return None
        if "." in col_name:
            return col_name
        for join_table in join_tables:
            prefix = f"{join_table}_"
            if col_name.startswith(prefix):
                suffix = col_name[len(prefix) :]
                if suffix:
                    return f"{join_table}.{suffix}"
        if base_alias and col_name in dataset_columns:
            return f"{base_alias}.{col_name}"
        if base_alias and col_name.startswith(f"{base_alias}_"):
            suffix = col_name[len(base_alias) + 1 :]
            if suffix:
                return f"{base_alias}.{suffix}"
        cube_table = self._infer_table_from_column(col_name)
        if cube_table:
            suffix = col_name[len(cube_table) + 1 :]
            if suffix:
                return f"{cube_table}.{suffix}"
        return col_name

    def _normalize_filters(self, filters: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for flt in filters:
            if not isinstance(flt, dict):
                continue
            raw_col, op, val = self._coerce_filter_fields(flt)
            normalized.append(
                {
                    "col": raw_col,
                    "op": op,
                    "val": self._unwrap_filter_value(val),
                }
            )
        return normalized

    def _collect_filters(self, query: dict[str, Any], form_data: Any) -> list[Any]:
        filters: list[Any] = []

        def extend_list(value: Any) -> None:
            if isinstance(value, list):
                filters.extend(value)

        def pull_from(obj: Any) -> None:
            if not isinstance(obj, dict):
                return
            extend_list(obj.get("filters"))
            extend_list(obj.get("extra_filters"))
            extend_list(obj.get("adhoc_filters"))
            extra_form = obj.get("extra_form_data")
            if isinstance(extra_form, dict):
                extend_list(extra_form.get("filters"))
                extend_list(extra_form.get("extra_filters"))

        if isinstance(query, dict):
            pull_from(query)
        pull_from(form_data)
        return filters

    def _coerce_filter_fields(self, flt: dict[str, Any]) -> tuple[Any, Any, Any]:
        raw_col = flt.get("col") or flt.get("subject") or flt.get("column")
        op = flt.get("op") or flt.get("operator")
        val = flt.get("val")
        if val is None:
            val = flt.get("comparator")
        return raw_col, op, val

    def _unwrap_filter_value(self, val: Any) -> Any:
        if isinstance(val, dict):
            for key in ("value", "values", "comparator", "val"):
                if key in val:
                    return val.get(key)
        return val

    def _extract_filter_col_name(self, raw: Any) -> str | None:
        if isinstance(raw, dict):
            return raw.get("sqlExpression") or raw.get("column_name") or raw.get("label")
        if isinstance(raw, str):
            return raw
        return None

    def _dedupe_filters(self, filters: list[Any]) -> list[Any]:
        seen: set[tuple[str, str, str]] = set()
        out: list[Any] = []
        for flt in filters:
            if not isinstance(flt, dict):
                continue
            key = (
                json.dumps(flt.get("col"), sort_keys=True, default=str),
                str(flt.get("op")),
                json.dumps(flt.get("val"), sort_keys=True, default=str),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(flt)
        return out

    def _resolve_dataset_schema(
        self, dataset_id: int | None
    ) -> dict[str, Any] | None:
        if not dataset_id:
            return None
        if dataset_id in self._dataset_cache:
            return self._dataset_cache[dataset_id]
        resolver = self._dataset_resolver
        schema = None
        if resolver:
            schema = resolver(dataset_id)
        elif self._settings:
            try:
                schema = fetch_dataset_schema(self._settings, dataset_id)
            except Exception:
                schema = None
        self._dataset_cache[dataset_id] = schema
        return schema

    def _resolve_cube_meta(self) -> dict[str, Any] | None:
        if self._cube_meta_cache is not None:
            return self._cube_meta_cache
        if not self._settings or not self._settings.cube_rest_url:
            return None
        try:
            meta = fetch_cube_meta(self._settings)
        except Exception:
            meta = None
        self._cube_meta_cache = meta
        return meta

    def _resolve_cube_schema(self) -> dict[str, Any] | None:
        if self._cube_schema_cache is not None:
            return self._cube_schema_cache
        meta = self._resolve_cube_meta()
        if not meta:
            return None
        schema = summarize_schema(meta)
        self._cube_schema_cache = schema
        return schema

    def _resolve_cube_conf_schema(self) -> dict[str, Any] | None:
        if self._cube_conf_cache is not None:
            return self._cube_conf_cache
        if not self._settings or not self._settings.cube_conf_path:
            return None
        try:
            schema = load_repo_schema(self._settings.cube_conf_path)
        except Exception:
            schema = None
        self._cube_conf_cache = schema
        return schema

    def _resolve_cube_joins(self) -> dict[str, dict[str, list[tuple[str, str]]]] | None:
        if self._cube_join_cache is not None:
            return self._cube_join_cache
        meta = self._resolve_cube_meta()
        if meta:
            joins = extract_join_map(meta)
        else:
            joins = {}
        conf_schema = self._resolve_cube_conf_schema()
        if conf_schema:
            for base, info in conf_schema.items():
                joined = info.get("joined") or {}
                for join_name, join_def in joined.items():
                    keys = join_def.get("joined_key") or []
                    pairs = []
                    for key in keys:
                        pairs.append((key.get("from_column"), key.get("to_column")))
                    if not pairs:
                        continue
                    joins.setdefault(base, {})
                    joins[base][join_name] = [
                        (pair[0], pair[1]) for pair in pairs if pair[0] and pair[1]
                    ]
        self._cube_join_cache = joins
        return joins

    def _infer_table_from_cube(self, columns: list[Any]) -> str | None:
        schema = self._resolve_cube_conf_schema() or self._resolve_cube_schema()
        if not schema:
            return None
        inferred: set[str] = set()
        for col in columns:
            col_name = (
                col
                if isinstance(col, str)
                else col.get("column_name")
                if isinstance(col, dict)
                else None
            )
            if not col_name or "." in col_name:
                continue
            table = self._infer_table_from_column(col_name)
            if table:
                inferred.add(table)
            else:
                for table_name, table_info in schema.items():
                    columns_set = set(table_info.get("columns") or [])
                    if col_name in columns_set:
                        inferred.add(table_name)
        if len(inferred) == 1:
            return next(iter(inferred))
        return None

    def _render_from_clause(self, table_name: str | None) -> str | None:
        if not table_name:
            return None
        joins = self._resolve_cube_joins() or {}
        if table_name not in joins:
            return f"FROM {table_name}"
        clauses = [f"FROM {table_name}"]
        for join_table, pairs in joins[table_name].items():
            on_exprs = []
            for left_col, right_col in pairs:
                on_exprs.append(
                    f"{table_name}.{left_col} = {join_table}.{right_col}"
                )
            if on_exprs:
                clauses.append(f"JOIN {join_table} ON " + " AND ".join(on_exprs))
        return " ".join(clauses)

    def _resolve_conf_entry(self, table_name: str | None) -> dict[str, Any] | None:
        if not table_name:
            return None
        schema = self._resolve_cube_conf_schema()
        if not schema:
            return None
        if table_name in schema:
            return schema.get(table_name)
        short = table_name.split(".")[-1]
        return schema.get(short)

    def _resolve_sql_table(self, cube_name: str) -> str:
        schema = self._resolve_cube_conf_schema()
        if schema and cube_name in schema:
            sql_table = schema[cube_name].get("sql_table")
            if sql_table:
                return sql_table
        return cube_name

    def _render_from_clause_from_entry(
        self, table_name: str | None, entry: dict[str, Any] | None
    ) -> tuple[str | None, set[str], str | None]:
        if not table_name:
            return None, set(), None
        if not entry:
            return table_name, set(), self._render_from_clause(table_name)

        join_tables: set[str] = set()
        base_alias = table_name
        base_table = table_name
        joins = entry.get("joined") or {}

        if entry.get("type") == "view":
            for j in joins.values():
                base_alias = j.get("from") or base_alias
                base_table = base_alias
                break

        base_sql = self._resolve_sql_table(base_table)
        clauses = [f"FROM {base_sql} AS {base_alias}"]
        for join_name, join_def in joins.items():
            join_alias = join_def.get("to") or join_name
            join_tables.add(join_alias)
            join_sql_table = self._resolve_sql_table(join_alias)
            keys = join_def.get("joined_key") or []
            on_exprs = []
            for key in keys:
                from_table = key.get("from_table") or base_alias
                to_table = key.get("to_table") or join_alias
                from_col = key.get("from_column")
                to_col = key.get("to_column")
                if from_col and to_col:
                    on_exprs.append(
                        f"{from_table}.{from_col} = {to_table}.{to_col}"
                    )
            if on_exprs:
                clauses.append(
                    f"JOIN {join_sql_table} AS {join_alias} ON "
                    + " AND ".join(on_exprs)
                )
        return base_alias, join_tables, " ".join(clauses)

    def _infer_table_from_column(self, col_name: str | None) -> str | None:
        if not col_name:
            return None
        schema = self._resolve_cube_conf_schema() or self._resolve_cube_schema()
        if not schema:
            return None
        for table_name, table_info in schema.items():
            joined = table_info.get("joined") or {}
            for join_table, join_info in joined.items():
                for col in join_info.get("columns") or []:
                    prefix = f"{join_table}_{col}"
                    if col_name == prefix:
                        return join_table
            for col in table_info.get("columns") or []:
                if col_name == col:
                    return table_name
                if col_name.startswith(f"{table_name}_"):
                    suffix = col_name[len(table_name) + 1 :]
                    if suffix and suffix == col:
                        return table_name
        return None

    def _cube_names(self) -> list[str]:
        meta = self._resolve_cube_meta()
        if not meta:
            return []
        cubes = meta.get("cubes")
        if not isinstance(cubes, list):
            return []
        names = []
        for cube in cubes:
            if isinstance(cube, dict) and cube.get("name"):
                names.append(cube["name"])
        return names


def fetch_dataset_schema(settings: Settings, dataset_id: int) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    response = session.get(f"{base_url}/api/v1/dataset/{dataset_id}", timeout=30)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Superset dataset response missing result")
    columns = result.get("columns") or []
    column_names = []
    if isinstance(columns, list):
        for col in columns:
            if isinstance(col, dict) and col.get("column_name"):
                column_names.append(col["column_name"])
    return {
        "id": dataset_id,
        "table_name": result.get("table_name") or result.get("table") or result.get("name"),
        "schema": result.get("schema"),
        "columns": column_names,
    }


def fetch_dataset_data(
    settings: Settings,
    query: dict[str, Any],
) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    _ensure_csrf(session, base_url)

    request_body = dict(query or {})
    datasource = request_body.get("datasource")
    if not datasource:
        raise ValueError("datasource.id is required")
    if isinstance(datasource, dict):
        datasource = dict(datasource)
        if "id" not in datasource:
            raise ValueError("datasource.id is required")
        datasource.setdefault("type", "table")
        request_body["datasource"] = datasource

    if not request_body.get("queries"):
        request_body["queries"] = [
            {
                "columns": request_body.get("columns") or [],
                "metrics": request_body.get("metrics") or [],
                "filters": request_body.get("filters") or [],
                "orderby": request_body.get("orderby") or [],
                "row_limit": request_body.get("row_limit"),
                "extras": request_body.get("extras") or {},
            }
        ]

    request_body.setdefault("result_format", "json")
    request_body.setdefault("result_type", "full")

    response = session.post(
        f"{base_url}/api/v1/chart/data",
        json=request_body,
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Superset chart data error {response.status_code}: {response.text}"
        )
    payload = response.json()
    data = []
    for item in payload.get("result", []):
        data.append(item.get("data"))
    return {"data": data, "raw": payload}


def _decode_position_json(position_json: Any) -> dict[str, Any]:
    if isinstance(position_json, str):
        try:
            position_json = json.loads(position_json)
        except json.JSONDecodeError:
            return {}
    if isinstance(position_json, str):
        try:
            position_json = json.loads(position_json)
        except json.JSONDecodeError:
            return {}
    if not isinstance(position_json, dict):
        return {}
    return position_json


def _compact_layout(position_json: Any) -> dict[str, Any]:
    position_json = _decode_position_json(position_json)
    compact: dict[str, Any] = {}
    for node_id, node in position_json.items():
        if not isinstance(node, dict):
            continue

        node_type = node.get("type")
        children = node.get("children")
        compact_node: dict[str, Any] = {
            "id": node.get("id") or node_id,
            "type": node_type,
            "children": children if isinstance(children, list) else [],
        }

        if node_type == "CHART":
            meta = node.get("meta")
            if isinstance(meta, dict) and "chartId" in meta:
                compact_node["meta"] = {"chartId": meta.get("chartId")}

        compact[str(node_id)] = compact_node
    return compact


def fetch_dashboard_layout(settings: Settings, dashboard_id: int) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    response = session.get(f"{base_url}/api/v1/dashboard/{dashboard_id}", timeout=30)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Superset dashboard response missing result")
    layout = _compact_layout(result.get("position_json") or {})
    return {"dashboard_id": dashboard_id, "layout": layout}


def _find_path_to_node(layout: dict[str, Any], target_id: str) -> list[str]:
    if target_id not in layout:
        return []
    root_id = "ROOT_ID" if "ROOT_ID" in layout else next(iter(layout.keys()), None)
    if not root_id:
        return []
    queue: list[tuple[str, list[str]]] = [(root_id, [root_id])]
    visited: set[str] = set()
    while queue:
        current_id, path = queue.pop(0)
        if current_id in visited:
            continue
        visited.add(current_id)
        if current_id == target_id:
            return path
        node = layout.get(current_id)
        if not isinstance(node, dict):
            continue
        children = node.get("children")
        if not isinstance(children, list):
            continue
        for child_id in children:
            child_key = str(child_id)
            if child_key in layout:
                queue.append((child_key, [*path, child_key]))
    return []


def _resolve_target_container_id(
    layout: dict[str, Any],
    dashboard_charts: list[dict[str, Any]],
    tab_id: str | None,
    tab_name: str | None,
) -> tuple[str, dict[str, Any] | None]:
    if tab_id:
        node = layout.get(tab_id)
        if isinstance(node, dict) and node.get("type") == "TAB":
            name = (node.get("meta") or {}).get("text") if isinstance(node.get("meta"), dict) else None
            return tab_id, {"id": tab_id, "name": name}
        raise ValueError(f"tab_id not found in layout: {tab_id}")

    if tab_name:
        want = tab_name.strip().lower()
        for node_key, node in layout.items():
            if not isinstance(node, dict) or node.get("type") != "TAB":
                continue
            meta = node.get("meta") or {}
            name = meta.get("text") if isinstance(meta, dict) else None
            if isinstance(name, str) and name.strip().lower() == want:
                return str(node_key), {"id": str(node_key), "name": name}
        raise ValueError(f"tab_name not found in layout: {tab_name}")

    for chart in dashboard_charts:
        tab = chart.get("tab")
        if isinstance(tab, dict) and tab.get("id"):
            tab_key = str(tab.get("id"))
            node = layout.get(tab_key)
            if isinstance(node, dict) and node.get("type") == "TAB":
                return tab_key, {"id": tab_key, "name": tab.get("name")}

    for node_key, node in layout.items():
        if isinstance(node, dict) and node.get("type") == "TAB":
            meta = node.get("meta") or {}
            name = meta.get("text") if isinstance(meta, dict) else None
            return str(node_key), {"id": str(node_key), "name": name}

    if "GRID_ID" in layout:
        return "GRID_ID", None
    for node_key, node in layout.items():
        if isinstance(node, dict) and node.get("type") == "GRID":
            return str(node_key), None
    raise RuntimeError("dashboard layout has no TAB/GRID container")


def append_chart_to_dashboard_layout(
    settings: Settings,
    dashboard_id: int,
    chart_id: int,
    tab_id: str | None = None,
    tab_name: str | None = None,
    width: int = 4,
    height: int = 50,
) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    _ensure_csrf(session, base_url)

    dashboard_resp = session.get(f"{base_url}/api/v1/dashboard/{dashboard_id}", timeout=30)
    if dashboard_resp.status_code == 404:
        raise LookupError("dashboard not found")
    dashboard_resp.raise_for_status()
    dashboard_payload = dashboard_resp.json()
    dashboard_result = (
        dashboard_payload.get("result") if isinstance(dashboard_payload, dict) else None
    )
    if not isinstance(dashboard_result, dict):
        raise RuntimeError("Superset dashboard response missing result")

    chart_detail = _fetch_chart_detail(session, base_url, chart_id)
    if not chart_detail:
        raise LookupError(f"chart {chart_id} not found")
    slice_name = chart_detail.get("slice_name") or chart_detail.get("name") or f"Chart {chart_id}"

    def _extract_dashboard_ids(value: Any) -> list[int]:
        if not isinstance(value, list):
            return []
        out: list[int] = []
        for item in value:
            if isinstance(item, dict):
                raw = item.get("id") or item.get("dashboard_id")
            else:
                raw = item
            try:
                if raw is not None:
                    out.append(int(raw))
            except (TypeError, ValueError):
                continue
        # Keep order stable and unique
        deduped: list[int] = []
        seen: set[int] = set()
        for dashboard_value in out:
            if dashboard_value in seen:
                continue
            seen.add(dashboard_value)
            deduped.append(dashboard_value)
        return deduped

    def _assign_chart_to_dashboard() -> None:
        existing_dashboard_ids = _extract_dashboard_ids(chart_detail.get("dashboards"))
        if dashboard_id in existing_dashboard_ids:
            return

        # Preferred when supported by Superset version.
        attach_resp = session.post(
            f"{base_url}/api/v1/dashboard/{dashboard_id}/charts",
            json={"chart_id": chart_id},
            timeout=30,
        )
        if attach_resp.status_code in {200, 201, 202, 204}:
            return

        updated_dashboard_ids = sorted({*existing_dashboard_ids, int(dashboard_id)})
        patch_resp = session.put(
            f"{base_url}/api/v1/chart/{chart_id}",
            json={"dashboards": updated_dashboard_ids},
            timeout=30,
        )
        if patch_resp.status_code in {200, 201, 202, 204}:
            return

        # Some versions require full chart payload on update.
        fallback_payload: dict[str, Any] = {
            "slice_name": slice_name,
            "viz_type": chart_detail.get("viz_type"),
            "datasource_id": chart_detail.get("datasource_id"),
            "datasource_type": chart_detail.get("datasource_type") or "table",
            "dashboards": updated_dashboard_ids,
        }
        if chart_detail.get("params") is not None:
            fallback_payload["params"] = chart_detail.get("params")
        owners = chart_detail.get("owners")
        if isinstance(owners, list):
            owner_ids: list[int] = []
            for owner in owners:
                if isinstance(owner, dict):
                    try:
                        owner_id = int(owner.get("id"))
                    except (TypeError, ValueError):
                        continue
                    owner_ids.append(owner_id)
            if owner_ids:
                fallback_payload["owners"] = owner_ids
        full_resp = session.put(
            f"{base_url}/api/v1/chart/{chart_id}",
            json=fallback_payload,
            timeout=30,
        )
        if full_resp.status_code in {200, 201, 202, 204}:
            return

        raise RuntimeError(
            "failed to attach chart to dashboard: "
            f"attach={attach_resp.status_code}, "
            f"patch={patch_resp.status_code}, "
            f"fallback={full_resp.status_code}"
        )

    _assign_chart_to_dashboard()

    layout = _decode_position_json(dashboard_result.get("position_json") or {})

    for node in layout.values():
        if not isinstance(node, dict) or node.get("type") != "CHART":
            continue
        meta = node.get("meta")
        if isinstance(meta, dict) and str(meta.get("chartId")) == str(chart_id):
            return {
                "status": "already_exists",
                "dashboard_id": dashboard_id,
                "chart_id": chart_id,
                "container_id": None,
                "row_id": None,
                "chart_node_id": str(node.get("id")) if node.get("id") else None,
                "tab": None,
            }

    dashboard_charts = fetch_dashboard_charts(settings, dashboard_id)
    container_id, tab_info = _resolve_target_container_id(
        layout=layout,
        dashboard_charts=dashboard_charts,
        tab_id=tab_id,
        tab_name=tab_name,
    )

    container = layout.get(container_id)
    if not isinstance(container, dict):
        raise RuntimeError(f"invalid container node: {container_id}")
    container_children = container.get("children")
    if not isinstance(container_children, list):
        container_children = []
        container["children"] = container_children

    row_id = f"ROW-{uuid.uuid4().hex[:20]}"
    chart_node_id = f"CHART-{uuid.uuid4().hex[:20]}"

    container_path = _find_path_to_node(layout, container_id)
    row_parents = container_path[:] if container_path else [container_id]
    chart_parents = [*row_parents, row_id]

    container_children.append(row_id)
    layout[row_id] = {
        "id": row_id,
        "type": "ROW",
        "children": [chart_node_id],
        "parents": row_parents,
        "meta": {"background": "BACKGROUND_TRANSPARENT"},
    }
    normalized_width = max(int(width), 1)
    # Superset dashboard layout height is grid-unit style (not px).
    # Values like 4/6 make charts nearly unreadable; keep a sane minimum.
    normalized_height = int(height)
    if normalized_height < 20:
        normalized_height = 50

    layout[chart_node_id] = {
        "id": chart_node_id,
        "type": "CHART",
        "children": [],
        "parents": chart_parents,
        "meta": {
            "chartId": chart_id,
            "sliceName": slice_name,
            "uuid": str(uuid.uuid4()),
            "width": normalized_width,
            "height": normalized_height,
        },
    }

    update_body: dict[str, Any] = {
        "position_json": json.dumps(layout, ensure_ascii=False),
    }
    if "json_metadata" in dashboard_result:
        update_body["json_metadata"] = dashboard_result.get("json_metadata")

    update_resp = session.put(
        f"{base_url}/api/v1/dashboard/{dashboard_id}",
        json=update_body,
        timeout=30,
    )
    if update_resp.status_code == 404:
        raise LookupError("dashboard not found")
    if update_resp.status_code >= 400:
        raise RuntimeError(
            f"Superset dashboard update error {update_resp.status_code}: {update_resp.text}"
        )

    return {
        "status": "appended",
        "dashboard_id": dashboard_id,
        "chart_id": chart_id,
        "container_id": container_id,
        "row_id": row_id,
        "chart_node_id": chart_node_id,
        "tab": tab_info,
    }


def _extract_chart_id(node: dict[str, Any]) -> int | str | None:
    for key in ("chart_id", "slice_id", "sliceId", "chartId"):
        if key in node:
            return node.get(key)
    return None


def _extract_tab_info(node: dict[str, Any]) -> dict[str, Any] | None:
    tab_id = node.get("tab_id") or node.get("tabId") or node.get("id")
    tab_name = (
        node.get("tab_title")
        or node.get("tabName")
        or node.get("title")
        or node.get("name")
        or node.get("label")
    )
    if tab_id is None and tab_name is None:
        return None
    return {"id": tab_id, "name": tab_name}


def _extract_default_tab_from_tabs(tabs_payload: Any) -> dict[str, Any] | None:
    def walk(node: Any) -> dict[str, Any] | None:
        if isinstance(node, list):
            for child in node:
                found = walk(child)
                if found:
                    return found
            return None
        if not isinstance(node, dict):
            return None
        node_type = str(node.get("type") or node.get("component_type") or node.get("node_type") or "")
        candidate = _extract_tab_info(node)
        if candidate and "tab" in node_type.lower():
            return candidate
        for key in ("children", "tabs", "items", "nodes", "contents", "elements", "layout"):
            value = node.get(key)
            if isinstance(value, list):
                found = walk(value)
                if found:
                    return found
            elif isinstance(value, dict):
                found = walk(value)
                if found:
                    return found
        return None

    return walk(tabs_payload)


def _extract_tab_map_from_tabs(tabs_payload: Any) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}

    def walk(node: Any, current_tab: dict[str, Any] | None) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, current_tab)
            return
        if not isinstance(node, dict):
            return

        node_type = str(node.get("type") or node.get("component_type") or node.get("node_type") or "")
        tab_info = current_tab
        candidate = _extract_tab_info(node)
        is_tab_node = (
            "tab" in node_type.lower()
            or "tab_title" in node
            or "tabName" in node
            or "tab_name" in node
        )
        if candidate and is_tab_node:
            tab_info = candidate

        chart_id = _extract_chart_id(node)
        if chart_id is not None and tab_info:
            mapping[str(chart_id)] = tab_info

        for key in ("children", "tabs", "items", "nodes", "contents", "elements", "layout"):
            value = node.get(key)
            if isinstance(value, list):
                for child in value:
                    walk(child, tab_info)
            elif isinstance(value, dict):
                walk(value, tab_info)

    walk(tabs_payload, None)
    return mapping


def _extract_tab_map_from_position_json(position_json: Any) -> dict[str, dict[str, Any]]:
    if position_json is None:
        return {}
    if isinstance(position_json, str):
        try:
            position_json = json.loads(position_json)
        except json.JSONDecodeError:
            return {}
    if not isinstance(position_json, dict):
        return {}

    mapping: dict[str, dict[str, Any]] = {}
    visited: set[str] = set()

    def is_tab(node: dict[str, Any]) -> bool:
        node_type = str(node.get("type") or "").upper()
        return node_type == "TAB" or node_type == "TABS" or "TAB" in node_type

    def get_tab_name(node: dict[str, Any], node_id: str) -> str:
        meta = node.get("meta") or {}
        if isinstance(meta, dict):
            name = meta.get("text") or meta.get("label") or meta.get("title") or meta.get("tabName")
            if name:
                return str(name)
        return str(node_id)

    def is_chart(node: dict[str, Any]) -> bool:
        node_type = str(node.get("type") or "").upper()
        if node_type == "CHART":
            return True
        if node.get("slice_id") is not None or node.get("chart_id") is not None:
            return True
        meta = node.get("meta") or {}
        if isinstance(meta, dict):
            return any(key in meta for key in ("sliceId", "slice_id", "chartId", "chart_id"))
        return False

    def get_slice_id(node: dict[str, Any], node_id: str) -> int | str | None:
        if node.get("slice_id") is not None:
            return node.get("slice_id")
        if node.get("chart_id") is not None:
            return node.get("chart_id")
        meta = node.get("meta") or {}
        if isinstance(meta, dict):
            for key in ("sliceId", "slice_id", "chartId", "chart_id"):
                if key in meta:
                    return meta.get(key)
        if isinstance(node_id, str) and node_id.upper().startswith("CHART-"):
            tail = node_id.split("-", 1)[1]
            if tail.isdigit():
                return int(tail)
        return None

    def dfs(node_id: str, current_tab: dict[str, Any] | None) -> None:
        if node_id in visited:
            return
        visited.add(node_id)
        node = position_json.get(node_id) or {}
        if not isinstance(node, dict):
            return

        tab_info = current_tab
        if is_tab(node):
            tab_info = {"id": node_id, "name": get_tab_name(node, node_id)}

        if tab_info and is_chart(node):
            chart_id = get_slice_id(node, node_id)
            if chart_id is not None and str(chart_id) not in mapping:
                mapping[str(chart_id)] = tab_info

        for child in node.get("children") or []:
            if isinstance(child, str):
                dfs(child, tab_info)

    root_id = "ROOT_ID" if "ROOT_ID" in position_json else next(iter(position_json.keys()), None)
    if isinstance(root_id, str):
        dfs(root_id, None)

    return mapping


def _fetch_dashboard_tabs(
    session: requests.Session,
    base_url: str,
    dashboard_id: int,
) -> Any:
    response = session.get(
        f"{base_url}/api/v1/dashboard/{dashboard_id}/tabs",
        timeout=30,
    )
    if response.status_code == 404:
        raise LookupError("dashboard not found")
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def fetch_dashboard_tab_map(
    settings: Settings,
    dashboard_id: int,
) -> dict[str, dict[str, Any]]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    tabs_payload = _fetch_dashboard_tabs(session, base_url, dashboard_id)
    return _extract_tab_map_from_tabs(tabs_payload)


def fetch_dashboard_default_tab(
    settings: Settings,
    dashboard_id: int,
) -> dict[str, Any] | None:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    tabs_payload = _fetch_dashboard_tabs(session, base_url, dashboard_id)
    return _extract_default_tab_from_tabs(tabs_payload)


class SupersetPoller:
    def __init__(
        self,
        *,
        meta_db_uri: str,
        poll_interval_sec: float,
        batch_size: int,
        dashboard_id: int | None,
        user_id: int | None,
        username: str | None,
        writer: DuckDBWriter,
        conn: duckdb.DuckDBPyConnection,
    ) -> None:
        self._meta_db_uri = self._normalize_db_uri(meta_db_uri)
        self._poll_interval_sec = poll_interval_sec
        self._batch_size = batch_size
        self._dashboard_id = dashboard_id
        self._user_id = user_id
        self._username = username
        self._writer = writer
        self._conn = conn
        self._stop = asyncio.Event()
        self._last_poll_at: datetime | None = None
        self._last_row_count: int | None = None
        self._last_error: str | None = None
        self._last_checkpoint_id: int | None = None
        ignore_raw = os.getenv(
            "SUPERSET_INGEST_IGNORE_ACTIONS",
            "ChartDataRestApi.json_dumps,DashboardRestApi.get,_get_data_response,fetch_rows",
        )
        self._ignored_actions = {
            item.strip()
            for item in ignore_raw.split(",")
            if item and item.strip()
        }

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self._can_connect():
            logger.warning("Superset poller disabled: psycopg2 not installed")
            return
        while not self._stop.is_set():
            await self.poll_once()
            await asyncio.sleep(self._poll_interval_sec)

    def _can_connect(self) -> bool:
        try:
            import psycopg2  # noqa: F401
        except Exception:
            return False
        return True

    def _normalize_db_uri(self, uri: str) -> str:
        if uri.startswith("postgresql+psycopg2://"):
            return uri.replace("postgresql+psycopg2://", "postgresql://", 1)
        return uri

    async def poll_once(self) -> None:
        self._last_poll_at = datetime.now(timezone.utc)
        self._last_error = None
        try:
            if self._user_id is None and self._username:
                self._user_id = await asyncio.to_thread(
                    self._resolve_user_id, self._username
                )

            last_id_raw = db.get_checkpoint(
                self._conn, db.CHECKPOINT_KEY_SUPERSET_LAST_ID
            )
            checkpoint_id = int(last_id_raw) if last_id_raw else 0
            # Keep poll cursor tied to upstream Superset ids only.
            # Derived/UI rows use local ids and must not advance this cursor.
            last_id = checkpoint_id
            # If checkpoint is ahead of upstream logs (from older buggy runs),
            # clamp it so polling can recover.
            source_max_id = await asyncio.to_thread(self._fetch_source_max_id)
            if source_max_id is not None and last_id > source_max_id:
                last_id = source_max_id
                await self._writer.enqueue_checkpoint(
                    db.CHECKPOINT_KEY_SUPERSET_LAST_ID, str(last_id)
                )
            self._last_checkpoint_id = last_id

            rows = await asyncio.to_thread(
                self._fetch_rows,
                last_id,
                self._dashboard_id,
                self._user_id,
            )
            self._last_row_count = len(rows)
            if not rows:
                logger.info("Superset poll tick: 0 rows (last_id=%s)", last_id)
                return

            max_id = last_id
            for row in rows:
                if row.get("action") in self._ignored_actions:
                    continue
                payload = self._normalize_row(row)
                await self._writer.enqueue_superset_log(payload)
                max_id = max(max_id, row["id"])

            await self._writer.enqueue_checkpoint(
                db.CHECKPOINT_KEY_SUPERSET_LAST_ID, str(max_id)
            )
            self._last_checkpoint_id = max_id
            logger.info(
                "Superset poll fetched %s rows (last_id=%s -> %s)",
                len(rows),
                last_id,
                max_id,
            )
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("Superset poll failed: %s", exc)

    async def sync_missing(self) -> int:
        self._last_poll_at = datetime.now(timezone.utc)
        self._last_error = None
        total_rows = 0
        try:
            if self._user_id is None and self._username:
                self._user_id = await asyncio.to_thread(
                    self._resolve_user_id, self._username
                )

            last_id_raw = db.get_checkpoint(
                self._conn, db.CHECKPOINT_KEY_SUPERSET_LAST_ID
            )
            last_id = int(last_id_raw) if last_id_raw else 0
            self._last_checkpoint_id = last_id
            while True:
                rows = await asyncio.to_thread(
                    self._fetch_rows,
                    last_id,
                    self._dashboard_id,
                    self._user_id,
                )
                if not rows:
                    break

                max_id = last_id
                for row in rows:
                    if row.get("action") in self._ignored_actions:
                        continue
                    payload = self._normalize_row(row)
                    await self._writer.enqueue_superset_log(payload)
                    max_id = max(max_id, row["id"])
                total_rows += len(rows)
                last_id = max_id

                await self._writer.enqueue_checkpoint(
                    db.CHECKPOINT_KEY_SUPERSET_LAST_ID, str(max_id)
                )
                self._last_checkpoint_id = max_id

            self._last_row_count = total_rows
            logger.info("Superset sync inserted %s rows", total_rows)
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("Superset sync failed: %s", exc)
        return total_rows

    def _fetch_rows(
        self,
        last_id: int,
        dashboard_id: int | None,
        user_id: int | None,
    ) -> list[dict[str, Any]]:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        sql, params = self._build_query(
            last_id=last_id,
            batch_size=self._batch_size,
            dashboard_id=dashboard_id,
            user_id=user_id,
        )
        with psycopg2.connect(self._meta_db_uri) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())

    def _resolve_user_id(self, username: str) -> int | None:
        return lookup_user_id(self._meta_db_uri, username)

    def _fetch_source_max_id(self) -> int | None:
        import psycopg2

        with psycopg2.connect(self._meta_db_uri) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT COALESCE(MAX(id), 0) FROM logs")
                row = cursor.fetchone()
                if not row:
                    return None
                try:
                    return int(row[0])
                except (TypeError, ValueError):
                    return None

    def _build_query(
        self,
        *,
        last_id: int,
        batch_size: int,
        dashboard_id: int | None,
        user_id: int | None,
    ) -> tuple[str, tuple[Any, ...]]:
        filters = ["id > %s"]
        params: list[Any] = [last_id]

        if dashboard_id is not None:
            filters.append("dashboard_id = %s")
            params.append(dashboard_id)
        if user_id is not None:
            filters.append("user_id = %s")
            params.append(user_id)

        where_clause = " AND ".join(filters)
        sql = (
            "SELECT id, dttm, action, user_id, dashboard_id, slice_id, "
            "duration_ms, referrer, json "
            "FROM logs "
            f"WHERE {where_clause} "
            "ORDER BY id "
            "LIMIT %s"
        )
        params.append(batch_size)
        return sql, tuple(params)

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return DuckDBWriter.build_superset_payload(
            superset_log_id=row["id"],
            dttm=row.get("dttm"),
            action=row.get("action"),
            user_id=row.get("user_id"),
            dashboard_id=row.get("dashboard_id"),
            slice_id=row.get("slice_id"),
            duration_ms=row.get("duration_ms"),
            referrer=row.get("referrer"),
            json_payload=row.get("json"),
            ingested_at=datetime.now(timezone.utc),
        )

    def status(self) -> dict[str, Any]:
        return {
            "last_poll_at": self._last_poll_at.isoformat()
            if self._last_poll_at
            else None,
            "last_row_count": self._last_row_count,
            "last_error": self._last_error,
            "last_checkpoint_id": self._last_checkpoint_id,
            "poll_interval_sec": self._poll_interval_sec,
            "batch_size": self._batch_size,
            "dashboard_id_filter": self._dashboard_id,
            "user_id_filter": self._user_id,
            "username_filter": self._username,
            "ignored_actions": sorted(self._ignored_actions),
        }


def lookup_user_id(meta_db_uri: str, username: str) -> int | None:
    import psycopg2

    with psycopg2.connect(meta_db_uri) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM ab_user WHERE username = %s LIMIT 1", (username,)
            )
            row = cursor.fetchone()
            return row[0] if row else None


def _fetch_dashboard_charts_endpoint(
    session: requests.Session,
    base_url: str,
    dashboard_id: int,
) -> list[dict[str, Any]]:
    response = session.get(
        f"{base_url}/api/v1/dashboard/{dashboard_id}/charts",
        timeout=30,
    )
    if response.status_code == 404:
        return []
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("result"), list):
        return payload["result"]
    return payload if isinstance(payload, list) else []


def _fetch_chart_detail(
    session: requests.Session,
    base_url: str,
    chart_id: int,
) -> dict[str, Any] | None:
    response = session.get(
        f"{base_url}/api/v1/chart/{chart_id}",
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        return payload["result"]
    return payload if isinstance(payload, dict) else None


def _extract_viz_type(chart_detail: dict[str, Any]) -> str | None:
    viz_type = chart_detail.get("viz_type")
    if viz_type:
        return str(viz_type)
    form_data = _extract_chart_form_data(chart_detail)
    if isinstance(form_data, dict):
        viz_type = form_data.get("viz_type")
        if viz_type:
            return str(viz_type)
    return None

def fetch_chart_form_data(settings: Settings, chart_id: int) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    detail = _fetch_chart_detail(session, base_url, chart_id)
    if not detail:
        raise LookupError(f"chart {chart_id} not found")
    form_data = detail.get("form_data")
    if form_data is None:
        params = detail.get("params")
        if isinstance(params, str) and params:
            try:
                form_data = json.loads(params)
            except json.JSONDecodeError:
                form_data = None
    return {"chart_id": chart_id, "form_data": form_data}


def fetch_chart_queries(settings: Settings, chart_id: int) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    detail = _fetch_chart_detail(session, base_url, chart_id)
    if not detail:
        raise LookupError(f"chart {chart_id} not found")
    queries = None
    form_data = detail.get("form_data")
    if form_data is None:
        params = detail.get("params")
        if isinstance(params, str) and params:
            try:
                form_data = json.loads(params)
            except json.JSONDecodeError:
                form_data = None
    if isinstance(form_data, dict):
        if isinstance(form_data.get("queries"), list):
            queries = form_data.get("queries")
        if isinstance(form_data.get("metrics"), list):
            form_data = dict(form_data)
            form_data["metrics"] = _normalize_metrics(form_data.get("metrics"))
    query_context = detail.get("query_context")
    if queries is None and isinstance(query_context, dict):
        if isinstance(query_context.get("queries"), list):
            queries = query_context.get("queries")
    if isinstance(queries, list):
        for query in queries:
            if isinstance(query, dict):
                _normalize_query_metrics(query)
    return normalize_chart_queries_result(
        {"chart_id": chart_id, "queries": queries, "form_data": form_data}
    )


def _extract_chart_form_data(chart_detail: dict[str, Any]) -> dict[str, Any]:
    form_data = chart_detail.get("form_data")
    if isinstance(form_data, dict):
        return form_data
    params = chart_detail.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            params = {}
    if isinstance(params, dict):
        return params.get("form_data") or params
    return {}


def fetch_chart_form_data(settings: Settings, chart_id: int) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    _ensure_csrf(session, base_url)

    detail = _fetch_chart_detail(session, base_url, chart_id)
    if not detail:
        raise LookupError(f"chart {chart_id} not found")
    return _extract_chart_form_data(detail)


def fetch_chart_data_from_log(
    settings: Settings,
    log_payload: dict[str, Any],
    dashboard_id: int | None = None,
) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    _ensure_csrf(session, base_url)

    payload = dict(log_payload or {})
    form_data = payload.get("form_data") if isinstance(payload, dict) else None
    if isinstance(form_data, str):
        try:
            form_data = json.loads(form_data)
        except json.JSONDecodeError:
            form_data = None
    request_body: dict[str, Any] = {
        "datasource": payload.get("datasource"),
        "queries": payload.get("queries"),
        "result_format": payload.get("result_format"),
        "result_type": payload.get("result_type"),
        "force": payload.get("force", False),
        "form_data": form_data,
    }
    if dashboard_id is not None:
        request_body["dashboard_id"] = dashboard_id

    response = session.post(
        f"{base_url}/api/v1/chart/data",
        json=request_body,
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Superset chart data error {response.status_code}: {response.text}"
        )
    data = []
    for item in response.json().get("result", []):
        data.append(item["data"])
    return data

def _expand_chart_ids(
    session: requests.Session,
    base_url: str,
    chart_ids: list[int],
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for chart_id in chart_ids:
        detail = _fetch_chart_detail(session, base_url, chart_id)
        if detail:
            expanded.append(detail)
    return expanded


def _normalize_charts(charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for chart in charts:
        if not isinstance(chart, dict):
            continue
        chart_id = chart.get("id") or chart.get("slice_id")
        normalized.append(
            {
                "chart_id": chart_id,
                "slice_id": chart.get("slice_id") or chart.get("id"),
                "name": chart.get("slice_name")
                or chart.get("name")
                or chart.get("chart_name"),
                "viz_type": chart.get("viz_type"),
                "datasource_id": chart.get("datasource_id")
                or (chart.get("datasource") or {}).get("id"),
                "datasource_type": chart.get("datasource_type")
                or (chart.get("datasource") or {}).get("type"),
            }
        )
    return normalized


def fetch_dashboard_charts(settings: Settings, dashboard_id: int) -> list[dict[str, Any]]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    response = session.get(
        f"{base_url}/api/v1/dashboard/{dashboard_id}",
        timeout=30,
    )
    if response.status_code == 404:
        raise LookupError("dashboard not found")
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") if isinstance(payload, dict) else None
    charts = result.get("charts") if isinstance(result, dict) else None
    position_json = result.get("position_json") if isinstance(result, dict) else None
    if isinstance(charts, dict) and isinstance(charts.get("result"), list):
        charts = charts["result"]
    if not charts:
        charts = _fetch_dashboard_charts_endpoint(session, base_url, dashboard_id)
    if isinstance(charts, list) and charts and not isinstance(charts[0], dict):
        charts = _fetch_dashboard_charts_endpoint(session, base_url, dashboard_id)
    if isinstance(charts, list) and charts and not isinstance(charts[0], dict):
        try:
            chart_ids = [int(item) for item in charts]
        except (TypeError, ValueError):
            chart_ids = []
        charts = _expand_chart_ids(session, base_url, chart_ids)
    if isinstance(charts, dict) and isinstance(charts.get("result"), list):
        charts = charts["result"]
    normalized = _normalize_charts(charts or [])
    tab_map: dict[str, dict[str, Any]] = {}
    try:
        tab_map = fetch_dashboard_tab_map(settings, dashboard_id)
    except Exception:
        tab_map = {}
    if not tab_map:
        tab_map = _extract_tab_map_from_position_json(position_json)
    for chart in normalized:
        needs_detail = (
            not chart.get("datasource_id")
            or not chart.get("datasource_type")
            or not chart.get("viz_type")
        )
        if not needs_detail:
            continue
        chart_id = chart.get("slice_id") or chart.get("chart_id")
        if not chart_id:
            continue
        detail = _fetch_chart_detail(session, base_url, int(chart_id))
        if not detail:
            continue
        chart["datasource_id"] = detail.get("datasource_id")
        chart["datasource_type"] = detail.get("datasource_type")
        chart["name"] = chart.get("name") or detail.get("slice_name") or detail.get("name")
        if not chart.get("viz_type"):
            chart["viz_type"] = _extract_viz_type(detail)
    for chart in normalized:
        chart_id = chart.get("slice_id") or chart.get("chart_id")
        chart["tab"] = tab_map.get(str(chart_id)) if chart_id is not None else None
    return normalized
