from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import duckdb
import requests
import sqlglot

from fastapi_service import db
from fastapi_service.config import Settings
from fastapi_service.cube import extract_join_map, fetch_cube_meta, summarize_schema
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
                where_exprs.append(f"({extra_where})")
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


def fetch_dashboard_layout(settings: Settings, dashboard_id: int) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    response = session.get(f"{base_url}/api/v1/dashboard/{dashboard_id}", timeout=30)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError("Superset dashboard response missing result")
    layout = result.get("position_json") or {}
    if isinstance(layout, str):
        try:
            layout = json.loads(layout)
        except json.JSONDecodeError:
            layout = {}
    return {"dashboard_id": dashboard_id, "layout": layout}


def extract_tab_map(layout: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not isinstance(layout, dict):
        return mapping
    for key, node in layout.items():
        if not isinstance(node, dict):
            continue
        meta = node.get("meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        if node.get("type") == "CHART":
            chart_id = meta.get("chartId") or meta.get("sliceId")
            tab_name = meta.get("tabName") or meta.get("tab_name")
            if chart_id is not None and tab_name:
                mapping[str(chart_id)] = str(tab_name)
    return mapping


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
            table_max_id = db.get_max_superset_log_id(self._conn)
            last_id = max(checkpoint_id, table_max_id)
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

            last_id = db.get_max_superset_log_id(self._conn)
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


def _require_setting(value: str | None, name: str) -> str:
    if not value:
        raise ValueError(f"{name} is not configured")
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


def fetch_chart_detail(settings: Settings, chart_id: int) -> dict[str, Any]:
    session = _api_session_with_bearer(settings)
    base_url = _get_base_url(settings)
    detail = _fetch_chart_detail(session, base_url, chart_id)
    if not detail:
        raise LookupError(f"chart {chart_id} not found")
    return {
        "chart_id": chart_id,
        "datasource_id": detail.get("datasource_id"),
        "datasource_type": detail.get("datasource_type"),
        "name": detail.get("slice_name") or detail.get("name"),
    }


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
    for chart in normalized:
        if chart.get("datasource_id") and chart.get("datasource_type"):
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
    return normalized
