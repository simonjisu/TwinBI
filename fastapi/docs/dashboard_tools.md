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

- get_chart_metadata
  Fetches chart metadata for the active chart via Superset API.
  Output: {"chart_id": int, "metadata": dict | null} or {"error": "..."}

- list_dashboard_charts
  Lists charts for the configured or most recent dashboard.
  Output: {"dashboard_id": int, "charts": [..]} or {"error": "..."}

- get_chart_data_by_id
  Fetches chart data for a specific chart id using the latest log payload.
  Input: chart_id (int)
  Output: {"chart_id": int, "data": dict} or {"error": "..."}

Typical usage patterns
- "What charts are on this dashboard?" -> list_dashboard_charts
- "What is this chart based on?" -> get_chart_sql or get_chart_metadata
- "Show the data behind this chart" -> get_active_chart_data

Notes
- These tools require Superset credentials and DuckDB logs configured in FastAPI.
- If there is no active chart context, use list_dashboard_charts and then
  get_chart_data_by_id as needed.
