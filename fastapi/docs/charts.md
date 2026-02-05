# Chart Creation API Design (FastAPI → Superset)

This is a template‑first API that keeps form_data stable and avoids passing raw Superset payloads around.

## 1) Templates list

**GET /superset/charts/templates**

Response:
```json
{
  "templates": {
    "table": {
      "viz_type": "table",
      "form_data": {
        "viz_type": "table",
        "query_mode": "aggregate",
        "groupby": [],
        "metrics": [],
        "row_limit": 10000
      }
    },
    "line": {
      "viz_type": "echarts_timeseries_line",
      "form_data": {
        "viz_type": "echarts_timeseries_line",
        "granularity_sqla": null,
        "time_grain_sqla": "P1D",
        "time_range": "No filter",
        "metrics": [],
        "groupby": []
      }
    },
    "bar": {
      "viz_type": "echarts_timeseries_bar",
      "form_data": {
        "viz_type": "echarts_timeseries_bar",
        "granularity_sqla": null,
        "time_grain_sqla": "P1D",
        "time_range": "No filter",
        "metrics": [],
        "groupby": []
      }
    },
    "pie": {
      "viz_type": "pie",
      "form_data": {
        "viz_type": "pie",
        "metric": null,
        "groupby": [],
        "row_limit": 10000
      }
    },
    "scatter": {
      "viz_type": "echarts_timeseries_scatter",
      "form_data": {
        "viz_type": "echarts_timeseries_scatter",
        "granularity_sqla": null,
        "time_grain_sqla": "P1D",
        "time_range": "No filter",
        "metrics": [],
        "groupby": []
      }
    }
  }
}
```

Template source file:
- `fastapi/chart_templates.json`

## 1.1) Compact summary

- **List templates:** `GET /superset/charts/templates` (optional `?viz_type=table|line|bar|pie|scatter`)
- **Create chart:** `POST /superset/charts`
- **viz_type mapping:** `line→echarts_timeseries_line`, `bar→echarts_timeseries_bar`, `scatter→echarts_timeseries_scatter`, `pie→pie`, `table→table`
- **Required fields:** `line|bar|scatter` need `time_column+metrics`; `pie` needs `metric/metrics+groupby`; `table` needs `metrics` (aggregate) or `all_columns` (raw)
- **Auto‑fills:** `datasource="{dataset_id}__table"`, resolves adhoc metrics from `"SUM(col)"` strings

## 2) Create chart (template based)

**POST /superset/charts**

Request (example):
```json
{
  "dataset_id": 28,
  "slice_name": "Total Sales by Brand",
  "viz_type": "bar",
  "encodings": {
    "metrics": ["total_sales_amount"],
    "groupby": ["dim_product_brand"]
  },
  "options": {
    "row_limit": 10000,
    "orderby": [["total_sales_amount", false]]
  },
  "owners": [2],
  "dashboard_id": 12
}
```

Behavior:
- Validate template constraints.
- Build Superset payload (`viz_type`, `datasource_id`, `datasource_type`, `params`).
- Call `POST /api/v1/chart/`.

Response (example):
```json
{
  "status": "created",
  "chart_id": 662,
  "slice_name": "Total Sales by Brand"
}
```

### Actual request schema (current implementation)

```json
{
  "dataset_id": 28,
  "slice_name": "Total Sales by Brand",
  "viz_type": "bar",
  "datasource_type": "table",
  "encodings": {
    "metrics": ["total_sales_amount"],
    "groupby": ["dim_product_brand"],
    "time_column": "dim_date_date"
  },
  "options": {
    "row_limit": 10000,
    "time_grain_sqla": "P1M",
    "time_range": "No filter"
  },
  "owners": [2],
  "dashboard_id": 12
}
```

Notes:
- `viz_type` must match a key in `fastapi/chart_templates.json` (table, line, bar, pie, scatter).
- `time_column` is mapped to `granularity_sqla`.
- Pie requires `metric` or `metrics` + `groupby`.
- Table supports `all_columns` (raw) or `metrics` (aggregate).

### Examples (ready to copy)

**Line (time series)**
```bash
curl -s -X POST "http://localhost:8000/superset/charts" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_id": 37,
    "slice_name": "Monthly Sales Trend",
    "viz_type": "line",
    "encodings": {
      "time_column": "dim_date_date",
      "metrics": ["SUM(total_receipts)"]
    },
    "options": {
      "time_grain_sqla": "P1M",
      "time_range": "No filter"
    }
  }'
```

**Bar**
```bash
curl -s -X POST "http://localhost:8000/superset/charts" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_id": 37,
    "slice_name": "Sales by Department",
    "viz_type": "bar",
    "encodings": {
      "time_column": "dim_date_date",
      "metrics": ["SUM(total_receipts)"],
      "groupby": ["dim_product_department"]
    },
    "options": {
      "time_grain_sqla": "P1M",
      "time_range": "No filter"
    }
  }'
```

**Pie**
```bash
curl -s -X POST "http://localhost:8000/superset/charts" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_id": 37,
    "slice_name": "Revenue by Category",
    "viz_type": "pie",
    "encodings": {
      "metric": "SUM(total_receipts)",
      "groupby": ["dim_product_category"]
    }
  }'
```

**Table (aggregate)**
```bash
curl -s -X POST "http://localhost:8000/superset/charts" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_id": 37,
    "slice_name": "Units by Department",
    "viz_type": "table",
    "encodings": {
      "metrics": ["SUM(total_units_sold)"],
      "groupby": ["dim_product_department"]
    },
    "options": {
      "row_limit": 1000
    }
  }'
```

**Table (raw)**
```bash
curl -s -X POST "http://localhost:8000/superset/charts" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_id": 37,
    "slice_name": "Raw Sales Table",
    "viz_type": "table",
    "encodings": {
      "all_columns": ["sale_id", "total_units_sold", "total_receipts"]
    },
    "options": {
      "row_limit": 1000
    }
  }'
```

## 3) Optional: raw form_data mode

**POST /superset/charts/raw**

Request (example):
```json
{
  "dataset_id": 28,
  "slice_name": "Monthly Sales Trend",
  "viz_type": "echarts_timeseries_line",
  "form_data": {
    "granularity_sqla": "date",
    "time_grain_sqla": "P1M",
    "metrics": ["total_sales_amount"]
  }
}
```

## 4) Chart types list (UI helper)

**GET /superset/charts/types**

Response (example):
```json
{
  "viz_types": ["bar", "line", "pie", "echarts_timeseries_line", "..."]
}
```

---

# About “form_data”

**Can we get form_data from the dataset API?**

No. `/api/v1/dataset` (and our `/superset/datasets/*` endpoints) only provide **schema metadata** (columns, types, etc.). They do **not** return chart form_data.

**What to do instead**

Use one of these:

1) **Template‑generated form_data (recommended)**
   - Maintain a minimal form_data template per chart type.
   - Fill in `metrics`, `groupby`, `granularity_sqla`, etc. from user input.

2) **Reuse form_data from an existing chart**
   - Fetch chart detail and copy its `params` / `form_data`, then modify a few fields.

3) **Derive from controlPanel defaults**
   - Read controlPanel definitions in `superset-frontend` and build defaults.
   - This is accurate but more work to keep in sync.

In short: **dataset APIs give you fields, not form_data**. Form_data should come from templates or existing chart examples.

---

# Minimal form_data templates (by chart type)

These are minimal payloads inferred from the Superset frontend control panels and standard form_data usage. They are intentionally small so the API can expand them with defaults.

Sources (control panels):
- `superset-frontend/plugins/plugin-chart-echarts/src/Timeseries/Regular/Line/controlPanel.tsx`
- `superset-frontend/plugins/plugin-chart-echarts/src/Timeseries/Regular/Bar/controlPanel.tsx`
- `superset-frontend/plugins/plugin-chart-echarts/src/Pie/controlPanel.tsx`
- `superset-frontend/plugins/plugin-chart-table/src/controlPanel.tsx`

## 1) Timeseries Line (`viz_type: "line"`)

```json
{
  "viz_type": "line",
  "granularity_sqla": "date",
  "time_grain_sqla": "P1D",
  "time_range": "No filter",
  "metrics": ["total_sales_amount"],
  "groupby": []
}
```

## 2) Timeseries Bar (`viz_type: "bar"`)

```json
{
  "viz_type": "bar",
  "granularity_sqla": "date",
  "time_grain_sqla": "P1D",
  "time_range": "No filter",
  "metrics": ["total_sales_amount"],
  "groupby": []
}
```

## 3) Pie (`viz_type: "pie"`)

From `Pie/controlPanel.tsx`, `metric` and `groupby` are required (see `controlOverrides` + `formDataOverrides`).

```json
{
  "viz_type": "pie",
  "metric": "total_sales_amount",
  "groupby": ["dim_product_category"],
  "row_limit": 100
}
```

## 4) Table (aggregated) (`viz_type: "table"`)

Uses `metrics` + `groupby` for aggregate tables.

```json
{
  "viz_type": "table",
  "metrics": ["total_sales_amount"],
  "groupby": ["dim_product_category"],
  "row_limit": 1000
}
```

## 5) Table (raw) (`viz_type: "table"`)

Uses `all_columns` for raw row display.

```json
{
  "viz_type": "table",
  "all_columns": ["sale_id", "total_units_sold", "total_receipts"],
  "row_limit": 1000
}
```

---

# Notes

- `granularity_sqla` must be a **temporal column** from the dataset (date/timestamp).
- `time_grain_sqla` values are ISO durations (e.g., `P1D`, `P1W`, `P1M`, `P1Y`).
- For bar/line charts, you can add `groupby` to split series by category.
- These are **minimal** templates; the backend can merge defaults for UI/formatting options.
