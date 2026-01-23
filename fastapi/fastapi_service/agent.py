from __future__ import annotations
import duckdb
import asyncio
import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from fastapi_service.superset import (
    fetch_chart_data_from_log,
    fetch_dashboard_charts,
    fetch_dataset_data,
    fetch_dataset_schema,
    fetch_chart_form_data,
    fetch_chart_queries,
)
from fastapi_service.cube import fetch_cube_meta, run_cube_query
from fastapi_service.cube_conf import load_repo_schema
# import sys
# proj_path = Path(__file__).resolve().parent.parent
# sys.path.append(str(proj_path))
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DOCS_ROOT = _PROJECT_ROOT / "fastapi" / "docs"
_DOC_FALLBACKS: dict[str, str] = {
    "dashboard_tools.md": """Dashboard Agent Tools

Overview
These tools help the agent inspect Superset dashboards and charts and fetch
data for the active chart. Use them when a user asks about the current dashboard,
available charts, or chart results.

Tools
- get_active_chart_log
  Returns the latest Superset log payload for the active chart from DuckDB.
  Output: {"chart_id": int, "payload": dict | null} or {"error": "..."}

- get_active_chart_data
  Fetches chart data using the latest log payload for the active chart.
  Output: {"chart_id": int, "data": dict} or {"error": "..."}

- get_chart_sql
  Returns the latest SQL/query for the active chart from DuckDB logs.
  Output: {"chart_id": int, "sql": str | null} or {"error": "..."}

- get_activated_chart_metadata
  Fetches chart metadata for the active chart via Superset API.
  Output: {"chart_id": int, "metadata": dict | null} or {"error": "..."}

- list_dashboard_charts
  Lists charts for the configured or most recent dashboard.
  Output: {"dashboard_id": int, "charts": [..]} or {"error": "..."}

- get_superset_dataset_schema
  Returns Superset dataset schema metadata for a dataset id.
  Input: dataset_id (int)
  Output: {"id": int, "table_name": str, "schema": str | null, "columns": [..]} or {"error": "..."}

- query_superset_dataset
  Queries Superset dataset data with filters via /api/v1/chart/data.
  Input: dataset_id (int), query_json (str; JSON object)
  Output: {"data": [...], "raw": {...}} or {"error": "..."}

- get_chart_data_by_id
  Fetches chart data for a specific chart id using the latest log payload.
  Input: chart_id (int)
  Output: {"chart_id": int, "data": dict} or {"error": "..."}

- get_chart_form_data
  Returns chart form_data (parsed from chart params when needed).
  Input: chart_id (int)
  Output: {"chart_id": int, "form_data": dict | null} or {"error": "..."}

Documentation tools
- read_dashboard_tools_doc
  Loads this dashboard tools document.
- read_schema_explorer_doc
  Loads the schema explorer tools document.
- read_cube_tools_doc
  Loads the Cube tools document.

Typical usage patterns
- "What charts are on this dashboard?" -> list_dashboard_charts
- "What is this chart based on?" -> get_chart_sql or get_activated_chart_metadata
- "Show the data behind this chart" -> get_active_chart_data
- "Query a dataset with filters" -> query_superset_dataset(query_json)

Notes
- These tools require Superset credentials and DuckDB logs configured in FastAPI.
- If there is no active chart context, use list_dashboard_charts and then
  get_chart_data_by_id as needed.
- list_dashboard_charts returns datasource_id and datasource_type; datasource_id
  is the Superset dataset id and can be used with get_superset_dataset_schema
  and query_superset_dataset.
- Hint: if you need the chart's query structure (columns/metrics/filters) before
  issuing a dataset query, call get_chart_form_data and reuse its form_data to
  build the query payload.

Superset dataset query flow (recommended)
1) list_dashboard_charts -> find chart_id and datasource_id (dataset id)
2) get_chart_form_data(chart_id) -> read form_data (columns/metrics/filters)
3) query_superset_dataset(dataset_id, query_json) -> send ChartDataRestApi.data payload

Example query_json (extras.where with CAST)
```json
{
  "datasource": {
    "id": 28,
    "type": "table"
  },
  "queries": [
    {
      "columns": ["dim_product_department"],
      "metrics": [
        {
          "aggregate": "SUM",
          "column": { "column_name": "previous_units" },
          "label": "SUM(previous_units)"
        },
        {
          "aggregate": "SUM",
          "column": { "column_name": "total_units_sold" },
          "label": "SUM(total_units_sold)"
        },
        {
          "aggregate": "MAX",
          "column": { "column_name": "qoq_growth_rate" },
          "label": "MAX(qoq_growth_rate)"
        }
      ],
      "filters": [],
      "extras": {
        "where": "dim_date_quarter_start >= CAST('2024-07-01 00:00:00' AS TIMESTAMP)",
        "having": ""
      },
      "orderby": [],
      "row_limit": 10000
    }
  ],
  "result_format": "json",
  "result_type": "full"
}
```

Superset dataset query input schema (query_json)
```json
{
  "columns": [
    "string"
  ],
  "metrics": [
    "string"
  ],
  "filters": [
    {
      "additionalProp1": {}
    }
  ],
  "orderby": [
    "string"
  ],
  "row_limit": 0,
  "extras": {
    "additionalProp1": {}
  },
  "result_format": "table",
  "result_type": "full",
  "queries": [
    {
      "additionalProp1": {}
    }
  ]
}
```
""",
    "schema_explorer.md": """Schema Explorer Tools

Overview
These tools expose the SchemaExplorer graph for the star schema. Use them when
the user asks about fact tables, dimensions, measures, attributes, or whether
an attribute value exists.

Tools
- get_facts
  Returns the list of fact tables.
  Output: ["fact_sales", ...] or {"error": "..."}

- get_schema_info
  Returns schema nodes for a fact table.
  Input: fact_table (str)
  Output: list of nodes with:
  - name, type ("fact" or "dimension")
  - attributes (for dimensions)
  - measures and fks (for the fact table)

- search_attribute
  Searches hierarchy paths that lead to the attribute.
  Input: fact_table (str), attribute_name (str)
  Output: list of results with:
  - dimension, attribute
  - path: steps from dimension to attribute
  - stats: count, distinct_count, min/max, dtype, unique_values metadata

- search_value_exists
  Checks whether an attribute value exists.
  Input: fact_table (str), attribute_name (str), value (str)
  Output: true/false or {"error": "..."}

Typical usage patterns
- "What fact tables exist?" -> get_facts
- "What dimensions are in fact_sales?" -> get_schema_info("fact_sales")
- "Where is the year attribute?" -> search_attribute("fact_sales", "year")
- "Does year=2024 exist?" -> search_value_exists("fact_sales", "year", "2024")

Notes
- The explorer is configured to use the star schema under ./data/sales.
- Attribute lookup is case-insensitive for labels; use short attribute names.
""",
    "cube_tools.md": """Cube Tools

Overview
These tools use Cube's REST API and repository config to list cubes/views,
inspect schema, and query data. Use them when the user asks for data directly
from Cube or needs Cube member names.

Tools
- list_cube_tables
  Lists cubes and views from Cube config (preferred) and Cube meta.
  Output: [{"name": "...", "type": "cube|view"}] or {"error": "..."}

- get_cube_schema
  Returns schema for a cube or view.
  Input: table_name (str)
  Output (config-based): {"name": "...", "type": "cube|view", "columns": [...], "sql_table": "...", "joined": {...}}
  Output (meta-based cube): {"name": "...", "type": "cube", "measures": [...], "dimensions": [...], "segments": [...]}

- query_cube
  Executes a Cube query via /cubejs-api/v1/load.
  Input: query_json (str) - JSON string for a Cube query object or list.
  Output: raw Cube response payload or {"error": "..."}

Example query_json
{"measures":["sales.total_sales"],"dimensions":["sales.brand"],"limit":10}

Typical usage patterns
- "What cubes/views are available?" -> list_cube_tables
- "What fields exist in sales?" -> get_cube_schema("sales")
- "Run a Cube query for total sales by brand" -> query_cube(query_json)

Notes
- Requires CUBE_REST_URL for live queries and CUBE_CONF_PATH for repo schema.
- The query_json must be valid JSON.
""",
}
_SRC_ROOT = _PROJECT_ROOT / "src"
if _SRC_ROOT.exists():
    sys.path.insert(0, str(_SRC_ROOT))
if _PROJECT_ROOT.exists():
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from src.schema_processor import SchemaExplorer
except Exception as exc:  # pragma: no cover - optional dependency
    SchemaExplorer = None
    _SCHEMA_IMPORT_ERROR = exc
else:
    _SCHEMA_IMPORT_ERROR = None

try:
    from agents import Agent, ModelSettings, Runner, function_tool, RunItemStreamEvent
except Exception as exc:  # pragma: no cover - optional dependency
    Agent = None
    ModelSettings = None
    Runner = None
    function_tool = None
    RunItemStreamEvent = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

class Answer(BaseModel):
    answer: dict[str, Any] = Field(..., description="Final answer.")


@dataclass
class AgentContext:
    settings: Any
    conn: Any
    chart_id: int | None = None
    chart_name: str | None = None
    chart_data: dict[str, Any] | None = None


_ACTIVE_CONTEXT: AgentContext | None = None


def _get_active_context() -> AgentContext | None:
    return _ACTIVE_CONTEXT


def _get_active_settings() -> tuple[Any | None, dict[str, Any] | None]:
    context = _get_active_context()
    if context is None:
        return None, {"error": "active context not set"}
    if not context.settings:
        return None, {"error": "settings not available"}
    return context.settings, None


def _read_doc_file(path: Path) -> str | dict[str, Any]:
    if not path.is_absolute() and not path.exists():
        fallback_paths = [
            _DOCS_ROOT / path.name,
            _PROJECT_ROOT / "docs" / path.name,
            _PROJECT_ROOT / "fastapi_service" / "docs" / path.name,
            _PROJECT_ROOT.parent / path,
        ]
        for base in (Path(__file__).resolve(), Path.cwd()):
            for parent in [base, *base.parents]:
                fallback_paths.append(parent / "fastapi" / "docs" / path.name)
                fallback_paths.append(parent / "docs" / path.name)
        for candidate in fallback_paths:
            if candidate.exists():
                path = candidate
                break
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        fallback = _DOC_FALLBACKS.get(path.name)
        if fallback:
            return fallback
        return {"error": f"doc not found: {path}"}
    except Exception as exc:
        return {"error": f"failed to read doc: {exc}"}


def _fetch_latest_chart_log_payload(
    conn: duckdb.DuckDBPyConnection,
    chart_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT json
        FROM superset_action_logs
        WHERE slice_id = ? AND action IN ('ChartDataRestApi.data', 'ChartDataRestApi.json_dumps')
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        [chart_id],
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def _fetch_latest_dashboard_id(conn: duckdb.DuckDBPyConnection) -> int | None:
    row = conn.execute(
        """
        SELECT dashboard_id
        FROM superset_action_logs
        WHERE dashboard_id IS NOT NULL
        ORDER BY superset_log_id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return row[0]


if function_tool:
    _SCHEMA_EXPLORER_ERROR: Exception | None = None
    _SCHEMA_EXPLORER: SchemaExplorer | None = None
    if SchemaExplorer is None:
        _SCHEMA_EXPLORER_ERROR = _SCHEMA_IMPORT_ERROR
    else:
        try:
            _SCHEMA_EXPLORER = SchemaExplorer(Path("./data/sales"), schema_type="star")
        except Exception as exc:  # pragma: no cover - optional data dependency
            _SCHEMA_EXPLORER_ERROR = exc

    def _schema_explorer_or_error() -> tuple[SchemaExplorer | None, dict[str, Any] | None]:
        if _SCHEMA_EXPLORER is not None:
            return _SCHEMA_EXPLORER, None
        if _SCHEMA_EXPLORER_ERROR:
            return None, {"error": f"schema explorer not available: {_SCHEMA_EXPLORER_ERROR}"}
        return None, {"error": "schema explorer not available"}

    @function_tool
    def get_active_chart_log() -> dict[str, Any]:
        """
        Return the latest Superset log payload for the active chart.

        Input:
        - Uses the active context set by the API handler (chart id + DuckDB connection).

        Output:
        - {"chart_id": int, "payload": dict | null}
        - {"error": "..."} on missing chart id or missing payload.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if not context.chart_id:
            return {"error": "active chart id not set"}
        payload = _fetch_latest_chart_log_payload(context.conn, context.chart_id)
        return {"chart_id": context.chart_id, "payload": payload}

    @function_tool
    def get_active_chart_data() -> dict[str, Any]:
        """
        Fetch chart data from Superset using the latest log payload.

        Input:
        - Uses the active context set by the API handler (chart id + settings).

        Output:
        - {"chart_id": int, "data": dict} (raw Superset /api/v1/chart/data response)
        - {"error": "..."} on missing chart id or missing payload.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if not context.chart_id:
            return {"error": "active chart id not set"}
        payload = _fetch_latest_chart_log_payload(context.conn, context.chart_id)
        if not payload:
            return {"error": "no chart log payload found"}
        data = fetch_chart_data_from_log(context.settings, payload)
        return {"chart_id": context.chart_id, "data": data['raw']}

    @function_tool
    def get_chart_sql() -> dict[str, Any]:
        """
        Return the latest SQL/query for the active chart from DuckDB logs.

        Input:
        - Uses the active context set by the API handler (chart id + DuckDB connection).

        Output:
        - {"chart_id": int, "sql": str | null}
        - {"error": "..."} if no log payload or invalid JSON.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if not context.chart_id:
            return {"error": "active chart id not set"}
        row = context.conn.execute(
            """
            SELECT json
            FROM superset_action_logs
            WHERE slice_id = ? AND action IN ('ChartDataRestApi.data', 'ChartDataRestApi.json_dumps')
            ORDER BY superset_log_id DESC
            LIMIT 1
            """,
            [context.chart_id],
        ).fetchone()
        if not row or not row[0]:
            return {"error": "no chart log payload found"}
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return {"error": "log payload is not valid JSON"}
        return {"chart_id": context.chart_id, "sql": payload.get("sql") or payload.get("query")}

    @function_tool
    def get_activated_chart_metadata() -> dict[str, Any]:
        """
        Return chart metadata for the active chart via Superset API.

        Input:
        - Uses the active context set by the API handler (chart id + settings).

        Output:
        - {"chart_id": int, "metadata": dict | null}
        - {"error": "..."} if dashboard id is missing or chart not found.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if not context.chart_id:
            return {"error": "active chart id not set"}
        payload = _fetch_latest_chart_log_payload(context.conn, context.chart_id)
        dashboard_id = payload.get("dashboard_id") if isinstance(payload, dict) else None
        if not dashboard_id:
            return {"error": "dashboard id not found in log payload"}
        charts = fetch_dashboard_charts(context.settings, int(dashboard_id))
        for chart in charts:
            if str(chart.get("slice_id")) == str(context.chart_id):
                return {"chart_id": context.chart_id, "metadata": chart}
        return {"chart_id": context.chart_id, "metadata": None}

    @function_tool
    def list_dashboard_charts() -> dict[str, Any]:
        """
        List charts for the most relevant dashboard.

        Input:
        - Uses the active context to resolve the dashboard id:
          1) settings.superset_log_dashboard_id if configured
          2) latest dashboard id from DuckDB logs

        Output:
        - {"dashboard_id": int, "charts": [..]}
        - {"error": "..."} if no dashboard id is available.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        dashboard_id = context.settings.superset_log_dashboard_id
        if not dashboard_id:
            dashboard_id = _fetch_latest_dashboard_id(context.conn)
        if not dashboard_id:
            return {"error": "dashboard id not available"}
        charts = fetch_dashboard_charts(context.settings, int(dashboard_id))
        return {"dashboard_id": int(dashboard_id), "charts": charts}

    @function_tool
    def get_chart_data_by_id(chart_id: int) -> dict[str, Any]:
        """
        Fetch chart data for a specific chart id using the latest log payload.

        Input:
        - chart_id: Superset slice id (chart id).

        Output:
        - {"chart_id": int, "data": dict} raw Superset chart data response
        - {"error": "..."} if no log payload is found.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        payload = _fetch_latest_chart_log_payload(context.conn, chart_id)
        if not payload:
            return {"error": "no chart log payload found"}
        data = fetch_chart_data_from_log(context.settings, payload)
        return {"chart_id": chart_id, "data": data}

    @function_tool
    def read_dashboard_tools_doc() -> str | dict[str, Any]:
        """Return the dashboard tools documentation markdown."""
        return _read_doc_file(Path("fastapi/docs/dashboard_tools.md"))

    @function_tool
    def read_schema_explorer_doc() -> str | dict[str, Any]:
        """Return the schema explorer documentation markdown."""
        return _read_doc_file(Path("fastapi/docs/schema_explorer.md"))

    @function_tool
    def read_cube_tools_doc() -> str | dict[str, Any]:
        """Return the Cube tools documentation markdown."""
        return _read_doc_file(Path("fastapi/docs/cube_tools.md"))

    @function_tool
    def get_chart_form_data(chart_id: int) -> dict[str, Any]:
        """
        Return Superset chart form_data (parsed from chart params when needed).

        Input:
        - chart_id: Superset slice id (chart id).

        Output:
        - {"chart_id": int, "form_data": dict | null}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            return fetch_chart_form_data(settings, chart_id)
        except Exception as exc:
            return {"error": f"superset chart form_data fetch failed: {exc}"}

    @function_tool
    def get_chart_queries(chart_id: int) -> dict[str, Any]:
        """
        Return Superset chart query_context queries for a chart id.

        Input:
        - chart_id: Superset slice id (chart id).

        Output:
        - {"chart_id": int, "queries": list | null, "form_data": dict | null}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            return fetch_chart_queries(settings, chart_id)
        except Exception as exc:
            return {"error": f"superset chart queries fetch failed: {exc}"}

    @function_tool
    def get_superset_dataset_schema(dataset_id: int) -> dict[str, Any]:
        """
        Return Superset dataset schema metadata for a dataset id.

        Output:
        - {"id": int, "table_name": str, "schema": str | null, "columns": [..]}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            return fetch_dataset_schema(settings, dataset_id)
        except Exception as exc:
            return {"error": f"superset dataset schema fetch failed: {exc}"}

    @function_tool
    def query_superset_dataset(
        query_json: str,
    ) -> dict[str, Any]:
        """
        Query Superset dataset data with filters via /api/v1/chart/data.

        Input:
        - query_json: JSON string for ChartDataRestApi.data payload. Typically includes:
          datasource, queries (columns/metrics/filters/extras/orderby/row_limit),
          result_format, result_type.

        Output:
        - {"data": [...], "raw": {...}}
        - {"error": "..."} on missing settings or Superset API issues.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            return {"error": f"query_json is not valid JSON: {exc}"}
        if not isinstance(query, dict):
            return {"error": "query_json must be a JSON object"}
        try:
            return fetch_dataset_data(settings, query)
        except Exception as exc:
            return {"error": f"superset dataset query failed: {exc}"}

    @function_tool
    def list_cube_tables() -> list[dict[str, Any]] | dict[str, Any]:
        """
        List cubes and views available from Cube configuration or meta.

        Output:
        - [{"name": "...", "type": "cube|view"}]
        """
        settings, error = _get_active_settings()
        if error:
            return error

        entries: dict[str, dict[str, Any]] = {}
        if settings.cube_conf_path:
            schema = load_repo_schema(settings.cube_conf_path)
            for name, info in schema.items():
                entry_type = info.get("type") or "cube"
                entries[name] = {"name": name, "type": entry_type}

        if settings.cube_rest_url:
            meta = fetch_cube_meta(settings)
            for cube in meta.get("cubes", []) or []:
                if isinstance(cube, dict) and cube.get("name"):
                    name = cube["name"]
                    entries.setdefault(name, {"name": name, "type": "cube"})

        if not entries:
            return {"error": "cube configuration and meta not available"}
        return sorted(entries.values(), key=lambda item: (item.get("type"), item.get("name")))

    @function_tool
    def get_cube_schema(table_name: str) -> dict[str, Any]:
        """
        Return schema details for a cube or view.

        Output (conf-based):
        - {"name": "...", "type": "cube|view", "columns": [...], "sql_table": "...", "joined": {...}}

        Output (meta-based cube):
        - {"name": "...", "type": "cube", "measures": [...], "dimensions": [...], "segments": [...]}
        """
        settings, error = _get_active_settings()
        if error:
            return error

        if settings.cube_conf_path:
            schema = load_repo_schema(settings.cube_conf_path)
            entry = schema.get(table_name)
            if entry:
                entry_type = entry.get("type") or "cube"
                return {"name": table_name, "type": entry_type, **entry}

        if settings.cube_rest_url:
            meta = fetch_cube_meta(settings)
            for cube in meta.get("cubes", []) or []:
                if isinstance(cube, dict) and cube.get("name") == table_name:
                    measures = [m.get("name") for m in (cube.get("measures") or []) if m.get("name")]
                    dimensions = [d.get("name") for d in (cube.get("dimensions") or []) if d.get("name")]
                    segments = [s.get("name") for s in (cube.get("segments") or []) if s.get("name")]
                    joins = list((cube.get("joins") or {}).keys()) if isinstance(cube.get("joins"), dict) else []
                    return {
                        "name": table_name,
                        "type": "cube",
                        "measures": measures,
                        "dimensions": dimensions,
                        "segments": segments,
                        "joins": joins,
                    }

        return {"error": f"cube or view '{table_name}' not found"}

    @function_tool
    def query_cube(query_json: str) -> dict[str, Any]:
        """
        Run a Cube REST API query (/cubejs-api/v1/load).

        Input:
        - query_json: JSON string for a Cube query object (or list for data blending).

        Output:
        - Raw Cube response payload.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            return {"error": f"query_json is not valid JSON: {exc}"}
        if not isinstance(query, (dict, list)):
            return {"error": "query_json must be a JSON object or list"}
        try:
            return run_cube_query(settings, query)
        except Exception as exc:
            return {"error": f"cube query failed: {exc}"}

    @function_tool
    def get_facts() -> list[str] | dict[str, Any]:
        """
        Return the list of fact tables in the configured star schema.

        Example:
        - ["fact_sales"]
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.get_facts()

    @function_tool
    def get_schema_info(fact_table: str) -> list[dict] | dict[str, Any]:
        """
        Return the schema graph nodes for a fact table.

        Each node includes `name` and `type` ("fact" or "dimension").
        Dimension nodes include `attributes`; the fact node includes `measures` and `fks`.

        Example:
        - [{"name": "fact_sales", "type": "fact", "measures": [...], "fks": [...]},
           {"name": "dim_date", "type": "dimension", "attributes": [...]}, ...]
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.get_schema(fact_table)

    @function_tool
    def search_attribute(fact_table: str, attribute_name: str) -> list[dict] | dict[str, Any]:
        """
        Return hierarchy paths that lead to the attribute for the fact schema.

        Each result includes: dimension, attribute, path (dimension/level/attribute steps),
        and stats (count, distinct_count, min/max, dtype, unique_values metadata).
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.search_attribute(fact_table, attribute_name)

    @function_tool
    def search_value_exists(
        fact_table: str,
        attribute_name: str,
        value: str,
    ) -> bool | dict[str, Any]:
        """
        Return whether the attribute value exists for the given fact table and attribute.

        Example:
        - search_value_exists("fact_sales", "year", "2024") -> True
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.search_value(fact_table, attribute_name, value)

EXAMPLE = """
Example query_json
** For metrics: use the `aggregate(column_name)` format for labels **
```json
{
  "datasource": { "id": 28, "type": "table" },
  "queries": [
    {
      "columns": ["dim_product_department", "dim_product_category"],
      "metrics": [
        {
          "expressionType": "SIMPLE",
          "aggregate": "SUM",
          "column": { "column_name": "previous_units" },
          "label": "SUM(previous_units)"
        },
        {
          "expressionType": "SIMPLE",
          "aggregate": "SUM",
          "column": { "column_name": "total_units_sold" },
          "label": "SUM(total_units_sold)"
        },
        {
          "expressionType": "SIMPLE",
          "aggregate": "MAX",
          "column": { "column_name": "qoq_growth_rate" },
          "label": "MAX(qoq_growth_rate)"
        }
      ],
      "filters": [],
      "extras": {
        "where": "dim_date_quarter_start >= CAST('2024-07-01 00:00:00' AS TIMESTAMP)",
        "having": ""
      },
      "orderby": [["MAX(qoq_growth_rate)", true]],
      "row_limit": 10000
    }
  ],
  "result_format": "json",
  "result_type": "full"
}
"""

class AgentRunner:
    def __init__(self) -> None:
        self._router_agent = None
        self._init_lock = asyncio.Lock()
        self._init_error: Exception | None = None
        self._initialized = False
        self._dashboard_tools_doc: str | None = None

    @property
    def available(self) -> bool:
        return self._router_agent is not None

    async def startup(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            try:
                self._router_agent = self._build_agents()
                self._dashboard_tools_doc = self._load_dashboard_tools_doc()
            except Exception as exc:  # pragma: no cover - defensive init guard
                self._router_agent = None
                self._init_error = exc
            self._initialized = True

    def _build_agents(self) -> Any:
        if Agent is None or ModelSettings is None:
            return None

        model_settings = ModelSettings(
            reasoning={"effort": "medium"},
            verbosity="low",
            max_turns=100,
            response_format={"type": "json_object", "schema": Answer.model_json_schema()},
        )
        return Agent(
            name="Dashboard Agent",
            model=os.getenv("AGENT_MODEL", "gpt-5-nano"),
            tools=[
                get_active_chart_log,
                get_active_chart_data,
                get_chart_sql,
                get_activated_chart_metadata,
                list_dashboard_charts,
                get_chart_data_by_id,
                read_dashboard_tools_doc,
                read_schema_explorer_doc,
                read_cube_tools_doc,
                get_chart_queries,
                get_superset_dataset_schema,
                query_superset_dataset,
                list_cube_tables,
                get_cube_schema,
                query_cube,
                get_facts,
                get_schema_info,
                search_attribute,
                search_value_exists,
            ]
            if function_tool
            else [],
            model_settings=model_settings,
            instructions=(
                "[SYSTEM DATE] December 31st, 2024. "
                "You answer questions about the current dashboard and charts. "
                "Use the provided chart context when available. "
                "When user asking non-dashboard related questions, respond accordingly. "
                "When an active chart is available, fetch the active chart data and "
                "call `list_dashboard_charts` to see which charts will be helpful "
                "Then use `get_chart_data_by_id` to fetch data "
                "to explore the chart to give better answers "
                "If no active chart is available, explore dashboards by listing charts "
                "and fetching relevant chart data as needed. "
                "Use the schema explorer tools when the user asks about available "
                "tables, measures, dimensions, or values; or when you need to map "
                "a natural-language request to schema fields. "
                "Use the Cube tools to list cubes/views, inspect Cube schema, or "
                "run Cube queries when the user asks for data directly from Cube. "
                "When querying Superset datasets, **MUST** call get_chart_queries with the chart_id "
                "to obtain queries/form_data, then use query_superset_dataset. "
                f"add filters (row filtering), extras (WHERE clause), and columns (groupby). {EXAMPLE}"
                "Before using specialized tools, read the relevant documentation "
                "via read_dashboard_tools_doc, read_schema_explorer_doc, or "
                "read_cube_tools_doc to confirm input/output expectations at least once. "
                "When user is asking comparison questions between charts, you might need to query multiple times to the dataset or charts."
                "Return a JSON object with an 'answer' field containing the response."
            ),
        )

    async def respond(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
        context_obj: Any | None = None,
        debug: bool = False,
    ) -> tuple[str, list[dict[str, Any]], str | None]:
        await self.startup()
        if not self._router_agent:
            return (
                "Agent not available. "
                "Install the OpenAI agents package and set OPENAI_API_KEY."
            ), [{"type": "error", "message": "agent_not_available"}] if debug else [], None

        prompt = self._build_input_messages(
            message,
            history,
            context=self._inject_docs(context),
        )
        debug_items: list[dict[str, Any]] = []
        try:
            global _ACTIVE_CONTEXT
            _ACTIVE_CONTEXT = context_obj
            result = await self._run_agent(self._router_agent, prompt, context_obj)
        except Exception as exc:
            if debug:
                debug_items.append({"type": "error", "message": str(exc)})
            return "Agent call failed. Check API credentials and logs.", debug_items, None
        finally:
            _ACTIVE_CONTEXT = None
        answer = self._extract_answer(result)
        raw_output = self._extract_raw_output(result)
        if debug:
            debug_items.extend(self._extract_debug_items(result))
        return answer, debug_items, raw_output

    async def respond_stream(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
        context_obj: Any | None = None,
        debug: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        await self.startup()
        if not self._router_agent:
            yield {"event": "error", "message": "agent_not_available"}
            return

        prompt = self._build_input_messages(
            message,
            history,
            context=self._inject_docs(context),
        )
        last_text = ""
        try:
            global _ACTIVE_CONTEXT
            _ACTIVE_CONTEXT = context_obj
            stream = Runner.run_streamed(self._router_agent, prompt, context=context_obj)
            async for event in stream.stream_events():
                if RunItemStreamEvent and isinstance(event, RunItemStreamEvent):
                    name = event.name
                    item = event.item
                    if name == "reasoning_item_created":
                        yield {"event": "reasoning", "detail": "Reasoning ..."}
                    elif name == "tool_called":
                        tool_name = getattr(item.raw_item, "name", None)
                        tool_args = getattr(item.raw_item, "arguments", None)
                        yield {
                            "event": "tool_called",
                            "tool": tool_name,
                            "arguments": tool_args,
                        }
                    elif name == "tool_output":
                        output = getattr(item, "output", None)
                        yield {"event": "tool_output", "output": output}
                    elif name in ("message_output_created", "message_output_updated"):
                        raw_item = getattr(item, "raw_item", None)
                        text = None
                        if isinstance(raw_item, dict):
                            content = raw_item.get("content")
                            if isinstance(content, list) and content:
                                text = content[0].get("text")
                        if text is None:
                            text = getattr(item, "output_text", None)
                        if isinstance(text, str):
                            last_text = text
                            yield {"event": "message", "text": text}
        except Exception as exc:
            yield {"event": "error", "message": str(exc)}
            return
        finally:
            _ACTIVE_CONTEXT = None

        final_text = getattr(stream, "final_output", None) or last_text
        raw_text = final_text if isinstance(final_text, str) else str(final_text)
        answer = self._parse_json_answer(raw_text)
        yield {"event": "final", "answer": answer, "raw": raw_text}

    def _build_input_messages(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if context:
            messages.append({"role": "system", "content": context})
        for item in history[-20:]:
            role = (item.get("role") or "").strip()
            content = (item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            messages.append({"role": role, "content": content})
        if not (
            history
            and (history[-1].get("role") or "").strip() == "user"
            and (history[-1].get("content") or "").strip() == message.strip()
        ):
            messages.append({"role": "user", "content": message})
        return messages

    def _load_dashboard_tools_doc(self) -> str | None:
        doc = _read_doc_file(Path("fastapi/docs/dashboard_tools.md"))
        if isinstance(doc, str):
            return doc
        return None

    def _inject_docs(self, context: str | None) -> str | None:
        if not self._dashboard_tools_doc:
            return context
        prefix = f"[DOC] dashboard_tools.md\n{self._dashboard_tools_doc}\n"
        if context:
            return prefix + context
        return prefix

    async def _run_agent(self, agent: Any, prompt: str, context_obj: Any | None) -> Any:
        if Runner is None:
            raise RuntimeError(_IMPORT_ERROR or "agents Runner unavailable")

        run_sync = getattr(Runner, "run_sync", None)
        if callable(run_sync):
            return await asyncio.to_thread(run_sync, agent, prompt, context=context_obj)

        run_async = getattr(Runner, "run", None)
        if callable(run_async):
            result = run_async(agent, prompt, context=context_obj)
            if asyncio.iscoroutine(result):
                return await result
            return result

        raise RuntimeError("agents Runner has no run method")

    def _extract_answer(self, result: Any) -> str:
        if isinstance(result, str):
            return self._parse_json_answer(result)

        for attr in ("final_output", "output_text", "output"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return self._parse_json_answer(value)
        if isinstance(result, dict):
            return self._parse_json_answer(json.dumps(result))
        return str(result)

    def _parse_json_answer(self, text: str) -> str:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict):
            answer = payload.get("answer") or payload.get("message") or payload.get("text")
            if isinstance(answer, str) and answer.strip():
                return answer
        return text

    def _extract_raw_output(self, result: Any) -> str | None:
        if isinstance(result, str):
            return result
        for attr in ("final_output", "output_text", "output"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return value
        if isinstance(result, dict):
            try:
                return json.dumps(result)
            except TypeError:
                return str(result)
        return str(result) if result is not None else None

    def _extract_debug_items(self, result: Any) -> list[dict[str, Any]]:
        items = []
        new_items = getattr(result, "new_items", None)
        if not isinstance(new_items, list):
            final_text = self._extract_answer(result)
            if final_text:
                items.append({"type": "final_output", "final_output": final_text})
            return items
        doc_tools = {
            "read_dashboard_tools_doc": "dashboard tools",
            "read_schema_explorer_doc": "schema explorer",
            "read_cube_tools_doc": "cube tools",
        }
        for item in new_items:
            item_type = getattr(item, "type", None) or "unknown_item"
            if item_type == "reasoning_item":
                items.append({"type": item_type, "detail": "Reasoning ..."})
                continue
            raw = getattr(item, "raw_item", None)
            if item_type == "tool_call_item":
                name = getattr(raw, "name", None)
                if name is None and isinstance(raw, dict):
                    name = raw.get("name")
                if name in doc_tools:
                    items.append(
                        {
                            "type": item_type,
                            "name": f"reading {doc_tools[name]} document...",
                        }
                    )
                else:
                    items.append({"type": item_type, "name": name or "unknown_tool"})
                continue
            if item_type == "tool_call_output_item":
                output = None
                if isinstance(raw, dict):
                    output = raw.get("output")
                name = None
                if isinstance(raw, dict):
                    name = raw.get("name") or raw.get("tool_name")
                if name in doc_tools:
                    items.append({"type": item_type, "output": "documentation loaded"})
                else:
                    items.append({"type": item_type, "output": output})
                continue
            items.append({"type": item_type})
        final_text = self._extract_answer(result)
        if final_text:
            items.append({"type": "final_output", "final_output": final_text})
        return items
