Dashboard Agent Tools

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

- get_active_tab_charts
  Returns charts for the active tab (last tab click or default tab).
  Output: {"dashboard_id": int, "active_tab": {...}, "active_charts": [...], "last_ui_event": {...}}

- list_dashboard_charts
  Lists charts for the configured or most recent dashboard.
  Output: {"dashboard_id": int, "charts": [..]} or {"error": "..."}

- get_dashboard_layout
  Returns compact dashboard layout (`position_json` projection) for a dashboard id.
  Input: dashboard_id (int)
  Output: {"dashboard_id": int, "layout": {...}} or {"error": "..."}

- get_superset_dataset_schema
  Returns Superset dataset schema metadata for a dataset id.
  Input: dataset_id (int)
  Output: {"id": int, "table_name": str, "schema": str | null, "columns": [..]} or {"error": "..."}

- query_superset_dataset
  Queries Superset dataset data with filters via /api/v1/chart/data.
  Input: query_json (str; JSON object, ChartDataRestApi.data payload)
  Output: {"data": [...], "raw": {...}} or {"error": "..."}

Example (query_superset_dataset)
query_json:
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
          "expressionType": "SIMPLE",
          "aggregate": "SUM",
          "column": { "column_name": "total_units_sold" },
          "label": "SUM(total_units_sold)"
        }
      ],
      "filters": [
        { "col": "dim_date_quarter_start", "op": ">=", "val": "2024-07-01 00:00:00" }
      ],
      "row_limit": 1000
    }
  ],
  "result_format": "json",
  "result_type": "full"
}
```

Helper (adhoc metric)
```python
def make_adhoc_metric(column_name, aggregate, label=None):
    agg = aggregate.upper().strip()
    if not label:
        label = f"{agg}({column_name})"
    return {
        "expressionType": "SIMPLE",
        "aggregate": agg,
        "column": {"column_name": column_name},
        "label": label,
    }
```

- get_chart_data_by_id
  Fetches chart data for a specific chart id using the latest log payload.
  Input: chart_id (int)
  Output: {"chart_id": int, "data": dict} or {"error": "..."}

- get_chart_queries
  Returns chart query_context queries (and form_data if available).
  Input: chart_id (int)
  Output: {"chart_id": int, "queries": list | null, "form_data": dict | null} or {"error": "..."}

- create_superset_chart
  Creates a Superset chart from templates.
  Input: request_json (str; ChartCreateRequest JSON)
  Preferred inner JSON keys:
  - dataset_id (int)
  - slice_name (str)
  - viz_type (str; `line` | `bar` | `table` | `pie` | `scatter`)
  - encodings (object): `time_column`, `groupby`, `metrics`
  - options (object): `time_grain_sqla`, `time_range`, `row_limit`, ...
  Compatibility:
  - legacy `datasource` + `form_data` is accepted and normalized internally.
  - `viz_type` aliases like `echarts_timeseries_line` are normalized.
  Output: {"status":"created","chart_id":int|null,"slice_name":str,"warnings":[...]} or {"error":"..."}

- append_chart_to_dashboard
  Appends an existing chart to dashboard layout (`position_json`) by creating
  a new row/chart node and saving the dashboard.
  Input: request_json (str; {"dashboard_id":int,"chart_id":int,"tab_id"?:str,"tab_name"?:str,"width"?:int,"height"?:int})
  Output: {"status":"appended|already_exists",...} or {"error":"..."}

Documentation tools
- read_dashboard_tools_doc
  Loads this dashboard tools document.
- read_schema_explorer_doc
  Loads the schema explorer tools document.
- read_semantic_tools_doc
  Loads the semantic tools document.

Typical usage patterns
- "What charts are on this dashboard?" -> list_dashboard_charts
- "Show dashboard layout/tab ids" -> get_dashboard_layout(dashboard_id)
- "What is this chart based on?" -> get_chart_sql
- "Show the data behind this chart" -> get_active_chart_data
- "Query a dataset with filters" -> query_superset_dataset(query_json)
- "Create a chart and place it on dashboard tab" -> create_superset_chart, then append_chart_to_dashboard

Notes
- These tools require Superset credentials and DuckDB logs configured in FastAPI.
- If there is no active chart context, use list_dashboard_charts and then
  get_chart_data_by_id as needed.
- list_dashboard_charts returns datasource_id and datasource_type; datasource_id
  is the Superset dataset id and can be used with get_superset_dataset_schema
  and query_superset_dataset.
- Hint: if you need the chart's query structure (columns/metrics/filters) before
  issuing a dataset query, call get_chart_queries and reuse its queries/form_data
  to build the query payload.

create_superset_chart preferred request_json example
```json
{
  "dataset_id": 44,
  "slice_name": "Monthly Sales Trend",
  "viz_type": "line",
  "encodings": {
    "time_column": "dim_date_date",
    "groupby": ["dim_product_category"],
    "metrics": [
      {
        "expressionType": "SIMPLE",
        "aggregate": "SUM",
        "column": { "column_name": "total_receipts" },
        "label": "SUM(total_receipts)"
      }
    ]
  },
  "options": {
    "time_grain_sqla": "P1M",
    "time_range": "No filter",
    "row_limit": 10000
  }
}
```

Superset dataset query flow (recommended)
1) list_dashboard_charts -> find chart_id and datasource_id (dataset id)
2) get_chart_queries(chart_id) -> read queries/form_data (columns/metrics/filters)
3) query_superset_dataset(query_json) -> send ChartDataRestApi.data payload

Notes for /superset/datasets/{dataset_id}/query
Notes for /superset/datasets/query
- The endpoint mirrors Superset's /api/v1/chart/data payload.
- datasource.id is required; it is the Superset dataset id.

Example query_json
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
  "datasource": {
    "id": 28,
    "type": "table"
  },
  "queries": [
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
      }
    }
  ],
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
  "result_type": "full"
}
```
