# Charts Workflow via Function Tools

This document is for sub-agents that call function tools (not manual curl).

## Goal
Create a semantic view, sync a Superset dataset, create a chart, and append it to a dashboard.

## Tool Map
- `create_semantic_view_and_dataset(request_json)`
  - One-call semantic view + dataset sync.
  - Returns `chart_seed` (dataset id + column hints + sample chart payload).
- `get_dashboard_layout(dashboard_id)`
  - Returns compact dashboard layout (`position_json` reduced to key fields).
- `create_superset_chart(request_json)`
  - Creates chart from template-compatible payload.
- `append_chart_to_dashboard(request_json)`
  - One-call attach + append:
    1) ensures dashboard-chart relation exists,
    2) appends chart node to layout.

## Canonical Sequence
1. `create_semantic_view_and_dataset`
2. `get_dashboard_layout` (discover target `tab_id`)
3. `create_superset_chart`
4. `append_chart_to_dashboard`

## Payload Examples

### 0) `create_semantic_view_and_dataset` (preferred)
```json
{
  "request_json": "{\"view\":{\"view_name\":\"view_sales_qoq_by_product\",\"base_cube\":\"fact_sales\",\"description\":\"QoQ growth by product category/department with date context\",\"measures\":[\"qoq_growth_rate\",\"total_units_sold\",\"previous_units\",\"total_receipts\"],\"dimensions\":[{\"join_path\":\"fact_sales.dim_date\",\"includes\":[\"date\",\"quarter_start\",\"quarter\",\"year\"],\"prefix\":true},{\"join_path\":\"fact_sales.dim_product\",\"includes\":[\"department\",\"category\",\"brand\"],\"prefix\":true}],\"filters\":[],\"governance\":{\"allowlist\":[\"qoq_growth_rate\",\"total_units_sold\",\"previous_units\",\"quarter_start\",\"quarter\",\"year\",\"date\",\"department\",\"category\",\"brand\"]}},\"dataset\":{\"database_id\":2,\"schema\":\"public\",\"table_name\":\"view_sales_qoq_by_product\",\"force_refresh\":true,\"superset_username\":\"harry_potter\",\"superset_password\":\"1234\"}}"
}
```

### 1) `get_dashboard_layout`
```json
{
  "dashboard_id": 12
}
```

### 2) `create_superset_chart`
```json
{
  "request_json": "{\"dataset_id\":43,\"slice_name\":\"Monthly Sales Trend by Category\",\"viz_type\":\"line\",\"encodings\":{\"time_column\":\"dim_date_date\",\"groupby\":[\"dim_product_category\"],\"metrics\":[{\"expressionType\":\"SIMPLE\",\"aggregate\":\"SUM\",\"column\":{\"column_name\":\"total_receipts\"},\"label\":\"SUM(total_receipts)\"}]},\"options\":{\"time_grain_sqla\":\"P1M\",\"time_range\":\"No filter\"}}"
}
```

### 3) `append_chart_to_dashboard`
```json
{
  "request_json": "{\"dashboard_id\":12,\"chart_id\":946,\"tab_id\":\"TAB-gfLb86_MQoFqAy9UsN7qC\",\"width\":4,\"height\":50}"
}
```
`height` is dashboard layout unit (not px). Use around `50` for normal visibility.

## Expected Validation
After append, verify both:
- layout side has `meta.chartId = <new_chart_id>`
- dashboard chart-definition side includes `<new_chart_id>`

Tools to verify:
- `get_dashboard_layout(dashboard_id)`
- dashboard charts API endpoint (`GET /superset/dashboards/charts?dashboard_id=...`) if needed

## Notes
- Prefer `tab_id` from `get_dashboard_layout` for deterministic placement.
- `append_chart_to_dashboard` is the safe final step because it now synchronizes dashboard relation + layout in one call.
