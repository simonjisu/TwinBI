# BI Project Architecture (Streamlit + FastAPI + Cube + Superset + DuckDB Action Logging)

## 1. Purpose

This document describes an end-to-end architecture that adds a **FastAPI backend** to the existing Docker Compose stack in order to support:

- **LLM interactions** (chat → intent/NLQ → Cube query → result → response)
- **Unified action logging** across:
  - Streamlit chat messages
  - Superset Action Log (polled from Superset metadata DB)
  - Optional LLM request/response telemetry (future)

The design prioritizes:
- Minimal changes to existing Superset/Cube containers
- Operational simplicity (single “writer” for DuckDB)
- Traceability across UI ↔ LLM ↔ analytics events via `session_id` / `request_id`

---

## 2. Current Stack (from docker-compose)

### 2.1 Cube (Sales semantic layer)
Defined in `docker-compose.sales.yml`:

- Service: `cube` (container_name: `sales`)
- Image: `cubejs/cube:latest`
- Ports:
  - `34000:4000` (Cube REST API)
  - `35432:15432` (Cube PostgreSQL-compatible SQL interface)
- Storage: DuckDB file inside container:
  - `CUBEJS_DB_TYPE=duckdb`
  - `CUBEJS_DB_DUCKDB_DATABASE_PATH=/cube/data/sales.db`
- Volumes:
  - `./data/sales/database:/cube/data`
  - `./data/sales/cube_conf:/cube/conf`
- Network: `TwinBI🐝_net` (external)

**Implication:** Any container on `TwinBI🐝_net` can query Cube via:
- REST: `http://sales:4000`
- SQL (Postgres wire): `sales:15432`

---

### 2.2 Streamlit (UI)
Defined in `docker-compose.streamlit.yml`:

- Service: `streamlit` (container_name: `streamlit_app`)
- Exposes: `8501:8501`
- Volumes:
  - `./data:/app/cube_data:ro`
  - `./streamlit-app:/app`
- Environment (relevant):
  - `SUPERSET_PUBLIC_URL=http://localhost:8088`
  - `SUPERSET_INTERNAL_URL=http://superset_app:8088`
  - `SUPERSET_USERNAME=harry_potter`
  - `SUPERSET_PASSWORD=1234`
  - `SUPERSET_GUEST_AUD=superset`

**Implication:** Streamlit embeds Superset using the internal hostname `superset_app:8088`.

---

### 2.3 Superset (BI + Action Log source)
Defined in `docker-compose.superset.yml`:

Services:
- `superset` (container_name: `superset_app`) → `8088:8088`
- `db` (container_name: `superset_db`) → Postgres metadata DB (no host port exposed)
- `redis` (container_name: `superset_cache`) → cache/broker
- `superset-worker`, `superset-worker-beat`, `superset-init`

Superset services use env files:
- `docker/.env` (required)
- `docker/.env-local` (optional)

Volumes:
- `superset_home:/app/superset_home`
- `db_home:/var/lib/postgresql/data`
- `redis:/data`

**Key logging fact:** Superset Action Logs are stored in the Superset metadata DB in a `logs` table (commonly named `logs`).

---

## 3. Proposed Addition: FastAPI Backend

Add a new service (e.g., `api`) to the same network (`TwinBI🐝_net`) to act as:

1) **LLM Orchestrator**
- Accepts chat requests from Streamlit
- Calls LLM (OpenAI or internal endpoint)
- Calls Cube (REST or SQL)
- Returns structured results to Streamlit

2) **Unified Event Collector**
- Receives Streamlit chat logs
- Polls Superset metadata DB for Superset action logs
- Writes everything into DuckDB for analysis

3) **Single Writer Pattern for DuckDB**
DuckDB is excellent for analytics, but concurrent writes from multiple workers/processes can cause lock contention.  
To keep the system simple:
- Run FastAPI with **one worker**
- Centralize all DB writes through a **single writer queue** inside the FastAPI process

---

## 4. High-Level Data Flows

### 4.1 LLM interaction flow (chat → Cube → response)
1. User types question in Streamlit
2. Streamlit calls `POST /chat` on FastAPI with `{session_id, user_id, message, history}`
3. FastAPI:
   - uses the OpenAI Agents SDK with a **multi‑agent pipeline** (orchestrator + specialists)
   - injects chart context (active tab + active charts + last UI event) into the agent run
   - orchestrator delegates tool calls to specialist agents
   - returns the agent response as `answer`
4. FastAPI logs:
   - Streamlit chat payload (session_id, request_id, message, response, latency_ms)

### 4.2 Streamlit chat logging flow
1. User submits chat in Streamlit
2. Streamlit emits `POST /chat` with:
   - `session_id`, `user_id`, `message`, `history`, `active_chart_id`, `active_chart_name`
3. FastAPI writes to DuckDB `streamlit_chat_logs`

### 4.3 Superset Action Log ingestion flow (polling)
1. FastAPI background task runs every N seconds (e.g., 1–3s)
2. Poller queries `superset_db.logs` incrementally using `id > last_id`
3. New rows are appended into DuckDB
4. Checkpoint is updated (`last_id`)

> This enables “near real-time” Superset action visibility with **no Superset code changes**.

---

## 5. Components & Responsibilities

### 5.1 Streamlit (Presentation layer)
Responsibilities:
- Chat UI (prompt + render response)
- Visualizations (Plotly, etc.)
- Superset embedded dashboards (iframe)
- Send chat to FastAPI
- Schema graph sidebar (Plotly)

Integration points:
- `FASTAPI_INTERNAL_URL=http://api:8000` (recommended new env var)
- Existing Superset URLs remain unchanged

---

### 5.2 FastAPI (Backend Orchestration + Logging)
Responsibilities:
- `/chat`: Streamlit chat logging + multi-agent routing (normal vs dashboard)
- `/events`: UI event ingestion (writes to DuckDB)
- background: Superset log poller
- background: DuckDB writer

Suggested internal modules:
- `llm/` (provider adapters, prompt templates, tool calling)
- `semantic/` (REST + SQL clients)
- `logging/` (event schemas, queue, DuckDB writer)
- `superset/` (metadata DB poller, normalization)
- `api/` (FastAPI routers)

---

### 5.3 Cube (Semantic layer)
Responsibilities:
- Data modeling and metrics (via cube config)
- Serving REST and SQL interfaces
- Powered by DuckDB on disk (`sales.db`)

FastAPI should treat Cube as the “source of truth” for analytics queries.

---

### 5.4 Superset (BI + Embedded dashboards)
Responsibilities:
- Dashboards and charts
- Embedded access via Streamlit
- Emits actions into `superset_db.logs`

FastAPI uses Superset only as a **log source** (in the simplest design).

---

### 5.5 DuckDB (Unified analytics log store)
Responsibilities:
- Store all events locally as a single file (volume-mounted)
- Enable analysis with SQL (offline or exposed via API)

---

## 6. Interfaces

### 6.1 FastAPI endpoints

### POST /chat
Uses the OpenAI Agents SDK (`agents.Agent`) dashboard agent to respond. If unavailable, returns a fallback message.
Injects active chart context (chart id/name + latest Superset log form_data/queries from DuckDB) when available.

**Request (example)**

```json
{
  "session_id": "s_123",
  "user_id": "u_abc",
  "message": "hi",
  "history": [
    {"role": "user", "content": "hello"},
    {"role": "assistant", "content": "how can I help?"}
  ],
  "active_chart_id": 316,
  "active_chart_name": "Sales by Product",
  "debug": true
}
```

**Response (example)**

```json
{
  "session_id": "s_123",
  "request_id": "r_456",
  "answer": "Here is sales by category for 2025...",
  "query_plan": {
    "measures": ["Sales.amount"],
    "dimensions": ["Product.category"],
    "time_range": ["2025-01-01", "2025-12-31"]
  },
  "debug": [
    {
      "type": "context",
      "active_chart_id": 316,
      "active_chart_name": "Sales by Product",
      "chart_log": {"action": "ChartDataRestApi.data", "slice_id": 316},
      "chart_data": {"chart_data": "unavailable"}
    },
    {"type": "tool_call_item", "name": "get_active_chart_data"},
    {"type": "tool_call_output_item", "output": {"chart_id": 316, "data": {"...": "..."}}}
  ],
  "data": [
    {"category": "A", "amount": 12345},
    {"category": "B", "amount": 67890}
  ]
}
```

---

### POST /events

**Request**

```json
{
  "ts": "2026-01-12T10:15:00+09:00",
  "session_id": "s_123",
  "user_id": "u_abc",
  "event_type": "chart_click",
  "payload": {
    "chart_id": "sales_by_category",
    "value": "B"
  }
}
```

### GET /events/stream
Streams Superset logs (including embed UI events written into `superset_action_logs`) via SSE.

### GET /superset/charts/{chart_id}/data
Looks up the latest Superset log for `chart_id` in DuckDB, builds a chart payload
from the log `json` (`datasource`, `queries`, `result_format`, `result_type`, `force`,
optional `form_data`), then calls Superset `/api/v1/chart/data` and returns
`result[0].data`. If Superset responds as a list of datasets, the API returns the
first list as `data` and includes the full list in `raw`.

### GET /chat/context/latest
Returns the latest chart context stored during `/chat` processing (used by the UI
to display context logs).

### GET /chat/debug/latest
Returns the most recent debug payload from `/chat` when `debug: true` is used.

Response example:
```json
{
  "debug": {
    "session_id": "s_123",
    "request_id": "r_456",
    "items": [
      {"type": "context", "active_chart_id": 316},
      {"type": "tool_call_item", "name": "get_active_chart_data"}
    ]
  }
}
```

### GET /chat/dialogue
Returns the latest chat dialogue for a `session_id`, ordered by timestamp, with
paired user/assistant messages.

### GET /superset/charts/{chart_id}/log-context
Returns the latest Superset log payload for the chart (from DuckDB), including
`form_data` and `queries`.

**Response**

```
{"status":"ok"}
```

**Note**: `POST /events` writes embed UI events (e.g., tab clicks) into DuckDB `superset_action_logs`.

**Unit test (example)**

```python
payload = {
    "session_id": "s_1",
    "user_id": "u_1",
    "event_type": "chart_click",
    "payload": {"chart_id": "sales_by_category"},
}
response = client.post("/events", json=payload)
self.assertEqual(response.status_code, 200)
client.app.state.writer.flush_blocking()
```

---

### GET /superset/dashboards/charts

Returns charts by filter mode:
- `dashboard_id` only: all charts on that dashboard
- `user_name` / `user_id` only: all charts owned by that user (including unassigned charts with `dashboard_ids: []`)
- `dashboard_id` + `user_name` / `user_id`: only charts owned by user and assigned to that dashboard (intersection)

Notes:
- `username` is accepted as a backward-compatible alias for `user_name`.

**Response (example)**

```json
{
  "dashboard_id": 12,
  "charts": [
    {
      "chart_id": 34,
      "slice_id": 34,
      "name": "Sales by Category",
      "viz_type": "bar",
      "datasource_id": 5,
      "datasource_type": "table",
      "tab": {
        "id": 12,
        "name": "Overview"
      }
    }
  ]
}
```

**Owner-scope response (example)**

```json
{
  "dashboard_id": null,
  "user_id": 5,
  "user_name": "harry_potter",
  "count": 2,
  "charts": [
    {
      "chart_id": 704,
      "slice_id": 704,
      "name": "Average Daily Sales by State",
      "viz_type": "echarts_timeseries_line",
      "datasource_id": 42,
      "datasource_type": "table",
      "dashboard_ids": []
    }
  ]
}
```

Backward compatibility:
- `GET /superset/dashboards/{dashboard_id}/charts` is still supported and maps to dashboard-scope behavior.

**Unit test (example)**

```python
def test_superset_dashboard_charts_missing_config(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = self._build_settings(str(Path(tmpdir) / "events.duckdb"))
        app = create_app(settings)
        with TestClient(app) as client:
            response = client.get("/superset/dashboards/charts?dashboard_id=12")
            self.assertEqual(response.status_code, 400)
```

---

### GET /superset/dashboards/{dashboard_id}/layout

Returns a compact dashboard layout (parsed from Superset `position_json`) with the
minimum keys required for topology traversal and chart linking.

**Response (example)**

```json
{
  "dashboard_id": 12,
  "layout": {
    "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
    "GRID_ID": {"id": "GRID_ID", "type": "GRID", "children": ["TAB-..."]},
    "CHART-abc": {
      "id": "CHART-abc",
      "type": "CHART",
      "children": [],
      "meta": {"chartId": 662}
    }
  }
}
```

### POST /superset/dashboards/{dashboard_id}/layout/append-chart

Appends an existing chart to dashboard layout in one call:
1) attach chart to dashboard (dashboard-slice relation), then
2) write a new `ROW-*` and `CHART-*` node into `position_json`,
3) save via Superset dashboard update API.

**Request (example)**

```json
{
  "chart_id": 662,
  "tab_id": "TAB-4HTFsBSog_QWn2XhGESOD",
  "width": 4,
  "height": 50
}
```

**Response (example)**

```json
{
  "status": "appended",
  "dashboard_id": 12,
  "chart_id": 662,
  "container_id": "TAB-4HTFsBSog_QWn2XhGESOD",
  "row_id": "ROW-1b61d8c2e4b84785bdfd",
  "chart_node_id": "CHART-7468c7bdb7104c0a8d5d",
  "tab": {
    "id": "TAB-4HTFsBSog_QWn2XhGESOD",
    "name": "Sales"
  }
}
```

---

### GET /health

Used for container health checks.

---

### GET /poller/status

Returns the current Superset poller status and last checkpoint.

---

### POST /poller/reset

Clears the Superset log checkpoint.

---

### POST /poller/trigger

Runs a single Superset poll cycle immediately.

---

### POST /poller/sync

Backfills missing Superset logs and reports insert counts by type.

**Response (example)**

```json
{
  "status": "ok",
  "inserted": {
    "logs": 12,
    "chat": 0
  }
}
```

**Unit test (example)**

```python
sync = client.post("/poller/sync")
self.assertEqual(sync.status_code, 200)
self.assertIn(sync.json()["status"], {"ok", "disabled"})
inserted = sync.json().get("inserted", {})
self.assertIn("logs", inserted)
self.assertIn("chat", inserted)
```

---

### GET /superset/logs/latest

Returns recent Superset action logs from DuckDB, with optional filters.

**Query params**

- `dashboard_id`: filter by dashboard id
- `user_id`: filter by Superset user id
- `action`: filter by action name
- `limit`: max rows (default 50)

**Response (example)**

```json
[
  {
    "superset_log_id": 1001,
    "dttm": "2024-02-01T12:00:00Z",
    "action": "DashboardRestApi.get",
    "user_id": 1,
    "dashboard_id": 12,
    "slice_id": 34,
    "duration_ms": 120,
    "referrer": "http://localhost:8088/superset/dashboard/12/",
    "json": "{}",
    "ingested_at": "2024-02-01T12:00:01Z"
  }
]
```

**Unit test (example)**

```python
latest = client.get("/superset/logs/latest")
self.assertEqual(latest.status_code, 200)
self.assertIsInstance(latest.json(), list)
```

---

### GET /superset/logs/latest_sql

Returns the latest translated SQL for chart data logs.

**Query params**

- `dashboard_id`: filter by dashboard id
- `slice_id`: filter by chart id

**Response (example)**

```json
{
  "superset_log_id": 1203,
  "slice_id": 160,
  "sql": "SELECT dim_product_department, SUM(total_receipts) FROM table_26 GROUP BY dim_product_department LIMIT 5000"
}
```

**Unit test (example)**

```python
latest_sql = client.get("/superset/logs/latest_sql")
self.assertEqual(latest_sql.status_code, 404)
```

---

### GET /superset/datasets/{dataset_id}/schema

Returns Superset dataset metadata for SQL translation (table name + columns).

**Response (example)**

```json
{
  "id": 27,
  "table_name": "dim_product",
  "schema": null,
  "columns": ["dim_product_brand", "total_receipts"]
}
```

**Unit test (example)**

```python
schema = client.get("/superset/datasets/12/schema")
self.assertEqual(schema.status_code, 400)
```

---

### GET /semantic/schema

Returns Cube.js metadata from the Cube REST API (filtered to remove verbose keys).

**Response (example)**

```json
{
  "cubes": [
    {
      "name": "fact_sales",
      "measures": [],
      "dimensions": []
    }
  ]
}
```

**Unit test (example)**

```python
cube_schema = client.get("/semantic/schema")
self.assertEqual(cube_schema.status_code, 400)
```

---
### GET /events/stream

Streams Superset action logs and UI events from DuckDB using Server-Sent Events (SSE).

**Query params**

- `dashboard_id`: filter by dashboard id
- `user_id`: filter by Superset user id
- `action`: filter by action name
- `source`: `superset` (default)
- `last_id`: start from this superset_log_id (default 0)
- `limit`: max rows per poll (default 100)
- `poll_interval_sec`: poll interval (default 1.0)
- `Last-Event-ID` header: optional resume token used on reconnects (superset_log_id)

**Notes**

- When `action` is `ChartDataRestApi.data` or `ChartDataRestApi.json_dumps`, the stream payload includes `translated_sql`.
- The stream payload also includes `translated_filters` (list of filter dicts) and `translated_where` (rendered WHERE clauses).
- `action` values of `log` with `event_name` (e.g., `drill_by_modal_opened`) are surfaced as `action_label` and `event_name` in stream payloads.
- Drill-by apply events (`event_name: "further_drill_by"`) are labeled as `action_label: "drill_by"` and include a `drill_by` object (column, filters, depth).
- Superset drill-to-details requests (`json.path == "/datasource/samples"`) are labeled as `action_label: "drill_to_details"` with `sample_filters` when available.
- Filter extraction includes `filters`, `extra_filters`, `adhoc_filters`, and `extra_form_data` from both `form_data` and `queries`.
- SQL translation uses dataset metadata (table name + columns) when available.
- When dataset metadata is missing, SQL translation can infer table/column prefixes from Cube metadata (`CUBE_REST_URL` `/cubejs-api/v1/meta`) using `aliasMember` + cube joins.
- SQL translation uses Cube join metadata (join sql) to render JOIN clauses when available, and falls back to `CUBE_CONF_PATH` schema/joins.
- Cube metadata is fetched from `CUBE_REST_URL` (`/cubejs-api/v1/meta`); optional `CUBE_API_TOKEN` is sent as `Authorization`.

**Unit test (example)**

```python
with client.stream(
    "GET",
    "/events/stream",
    headers={"Last-Event-ID": "0"},
) as stream:
    self.assertEqual(stream.status_code, 200)
    content_type = stream.headers.get("content-type", "")
    self.assertTrue(content_type.startswith("text/event-stream"))
```

---

---

### GET /superset/users/lookup

Resolves a Superset username to user id (metadata DB lookup).

## 7. Logging & Correlation Strategy

### 7.1 Common identifiers

- **session_id**: stable across a user session in Streamlit  
- **request_id**: unique per `/chat` call  
- **superset_log_id**: Superset `logs.id` (source primary key) or local id for embed events

---

### 7.2 DuckDB tables (recommended)

#### streamlit_chat_logs

- `ts` TIMESTAMP  
- `session_id` VARCHAR  
- `request_id` VARCHAR  
- `user_id` VARCHAR  
- `message` VARCHAR  
- `response` VARCHAR  
- `latency_ms` BIGINT  

#### superset_action_logs

(ingested from `superset_db.logs`; keep raw + add ingestion columns)
Also stores embed UI events written by `POST /events` (action = event_type, json payload includes source/session/user).

- `superset_log_id` BIGINT  
- `dttm` TIMESTAMP  
- `action` VARCHAR  
- `user_id` BIGINT  
- `dashboard_id` BIGINT  
- `slice_id` BIGINT  
- `duration_ms` BIGINT  
- `referrer` VARCHAR  
- `json` VARCHAR  
- `ingested_at` TIMESTAMP  

#### _checkpoint

- `key` VARCHAR PRIMARY KEY  
- `value` VARCHAR  
  - e.g., last ingested Superset log id

---

## 8. Deployment Model (Compose)

### 8.1 New FastAPI service (conceptual)

Recommended runtime characteristics:

- `uvicorn` with **1 worker** (to avoid DuckDB write contention)
- Volume mount for DuckDB logs:
  - `./data/logs:/data`

Environment:

- `DUCKDB_PATH=/data/events.duckdb`
- `SUPERSET_META_DB_URI=postgresql://...@superset_db:5432/...`
- `SUPERSET_LOG_DASHBOARD_ID=<dashboard_id>` (optional filter)
- `SUPERSET_LOG_USER_ID=<user_id>` (optional filter)
- `SUPERSET_LOG_USERNAME=<username>` (optional filter)
- `CUBE_REST_URL=http://sales:4000`
- `CUBE_SQL_HOST=sales`
- `CUBE_SQL_PORT=15432`
- `LLM_PROVIDER=openai|internal`
- `OPENAI_API_KEY=...` (or internal endpoint config)

Network:

- same external network: `TwinBI🐝_net`

---

### 8.2 Streamlit updates

Add:

- `FASTAPI_INTERNAL_URL=http://api:8000`
- `FASTAPI_PUBLIC_URL=http://localhost:8000` (browser-facing for SSE/event posts)

Then:

- Chat requests go to FastAPI
- Superset logs are ingested by FastAPI

---

## 9. Superset Action Log Ingestion Details

### 9.1 Incremental polling approach

Query pattern:

```sql
SELECT ... FROM logs
WHERE id > :last_id
[AND dashboard_id = :dashboard_id]
[AND user_id = :user_id]
ORDER BY id
LIMIT :batch_size
```

Poll interval:

- 1–3 seconds for “near real-time”

Checkpoint:

- stored in DuckDB `_checkpoint` table

---

### 9.2 Why polling instead of Superset EVENT_LOGGER

- Polling requires **no Superset customization**
- EVENT_LOGGER can become “true streaming” (push) but requires Superset config + custom logger code
- Start simple with polling; upgrade later if needed

---

## 10. Minimal Sequence Diagrams (ASCII)

### 10.1 Chat + Cube query

```
User -> Streamlit: ask question
Streamlit -> FastAPI (/chat): session_id, message, history
FastAPI -> Agent: run LLM Agent
FastAPI -> DuckDB: write streamlit_chat_logs
FastAPI -> Streamlit: answer
```

### 10.2 Superset action log ingestion

```
FastAPI Poller -> superset_db: SELECT logs WHERE id > last_id
superset_db -> FastAPI Poller: rows
FastAPI Writer -> DuckDB: append superset_action_logs
FastAPI Writer -> DuckDB: update checkpoint
```
---

## 11. Chart Interaction Architecture (Current)

This section describes the current event and state model used to track chart interactions for embedded Superset.

### 11.0 Storage model and source of truth

All interaction and Superset events are unified in `events.duckdb` (mounted at `./data/logs/events.duckdb`), primarily in `superset_action_logs`.

`superset_action_logs` contains:
- polled Superset metadata DB log rows (`source` effectively server-side, via poller)
- UI-origin rows generated by `POST /events` (`json.source = "ui"`)

Key columns:
- `superset_log_id`, `dttm`, `action`, `dashboard_id`, `slice_id`, `json`, `ingested_at`

### 11.1 Event taxonomy in use

UI events emitted by the embed frontend and written through `/events`:
- `superset_tab_click`
- `chart_click`
- `legend_toggle`
- `cross_filter_added`
- `cross_filter_removed`
- `global_filter_added`
- `global_filter_removed`

Backward-compatible names still recognized by backend readers:
- `filter_added`
- `filter_removed`

Server-side Superset events used for query/filter context:
- `ChartDataRestApi.data`
- `ChartDataRestApi.json_dumps`

### 11.2 Frontend event generation (embed component)

Source: `streamlit-app/superset_embed_component/frontend/src/SupersetEmbed.tsx`

1. PostMessage events from iframe (`id="superset-ui-event"`) are mapped to:
   - `superset_tab_click`, `chart_click`, `legend_toggle`
2. DataMask snapshots are diffed to emit filter events:
   - chart/cross-filter diffs -> `cross_filter_added` / `cross_filter_removed`
   - native filter (`NATIVE_FILTER-*`) diffs -> `global_filter_added` / `global_filter_removed`
3. Events are posted to FastAPI `/events` with `session_id`, `event_type`, and payload.

### 11.3 FastAPI ingestion path

Source: `fastapi/fastapi_service/main.py`, `fastapi/fastapi_service/writer.py`

1. `/events` receives UI events.
2. `DuckDBWriter` converts UI payload into a `superset_action_logs` row:
   - `action = event_type`
   - `json = {"source":"ui","session_id":...,"event_type":...,"payload":...}`
3. Superset poller writes backend action logs into the same table.

This gives one ordered timeline for UI + backend events.

### 11.4 Stream contract (`GET /events/stream`)

- Streams rows from `superset_action_logs` by `superset_log_id`.
- Supports `source`, `dashboard_id`, `user_id`, `action`, `last_id`, `limit`.
- For chart-data actions, adds:
  - `translated_sql`
  - `translated_filters`
  - `translated_where`
- Emits `: keepalive` when no new rows.

Note:
- For active-context UIs, unfiltered stream consumption (`source=superset`) is safer than strict dashboard filtering because some UI rows can have sparse dashboard fields.

### 11.5 Active context contract (`GET /superset/charts/active`)

Source: `fastapi/fastapi_service/main.py` (`_build_superset_active_context`)

Returned shape:
- `dashboard_id`
- `active_tab`
- `active_charts` (tab scoped)
- `last_ui_event`
- `interacting_chart`
- `session_id`

Derivation rules:
1. Session resolution:
   - use query `session_id` if provided
   - otherwise use latest UI session id for the dashboard
2. Tab resolution:
   - latest `superset_tab_click` in session/dashboard
   - fallback to chart-derived/default tab
3. Chart list:
   - all charts in active tab
4. `active_charts[].filters`:
   - latest chart filters from `ChartDataRestApi.*` for each slice
   - merged with current global filters reconstructed from `global_filter_added/removed`
5. Filter cleanup:
   - entries with `"No filter"` are excluded
6. Interaction state:
   - `legend_toggle`, `chart_click`, `cross_filter_added` set interaction focus
   - `cross_filter_removed` clears interaction focus

### 11.6 Active context widget behavior

Source: `streamlit-app/javascripts/active_context.html`

The widget uses both:
- `/events/stream?source=superset` for realtime event reaction
- `/superset/charts/active` for canonical reconciliation

Session behavior:
- when Streamlit session id changes (browser refresh/new session), widget resets to idle
- events from other session ids are ignored when session id is available

### 11.7 Known edge cases

- `source_slice_id` can be `null` when upstream event payload has no chart/slice id.
- Global filter events should update chart filter state, but should not force chart focus text by themselves.
- Cross-filter source/target attribution is best-effort and may require combining UI events with `ChartDataRestApi.*`.

---

## 12. Component Relationship Summary (Request/Response View)

This section describes how the core components exchange requests and outputs.

### 12.1 LLM Agent ↔ FastAPI (REST API server)
- Request: Streamlit sends `POST /chat` or `POST /chat/stream` to FastAPI.
- Processing: FastAPI builds context, then invokes the **Orchestrator Agent**.
- Output: FastAPI returns the agent response (final answer or stream events).

### 12.1.1 Multi‑agent system (current)

| Agent | Responsibility | Tools |
| --- | --- | --- |
| Orchestrator | Route tasks, call specialist agents, assemble final response | `run_chart_manager_agent`, `run_schema_explorer_agent`, `run_answer_composer_agent`, `run_docs_retriever_agent` |
| ChartManager | Retrieve/manage chart context, query chart/dataset data, create chart and append to dashboard, run one-call semantic view+dataset sync when needed | `get_active_chart_log`, `get_chart_sql`, `get_active_tab_charts`, `list_dashboard_charts`, `get_dashboard_layout`, `list_superset_datasets`, `get_chart_form_data`, `get_chart_queries`, `get_superset_dataset_schema`, `query_superset_dataset`, `get_active_chart_data`, `get_chart_data_by_id`, `query_cube`, `list_chart_templates`, `list_superset_databases_meta`, `list_superset_database_tables`, `create_semantic_view_and_dataset`, `create_superset_chart`, `append_chart_to_dashboard` |
| SchemaExplorer | Map business terms to schema fields and validate field/value availability | `get_facts`, `get_schema_info`, `search_attribute`, `search_value_exists`, `list_cube_tables`, `get_cube_schema`, `get_semantic_schema` |
| Answer Composer | Draft final answer | (no tools) |
| DocsRetriever | Summarize docs + tool usage rules | `read_dashboard_tools_doc`, `read_schema_explorer_doc`, `read_semantic_tools_doc`, `read_charts_doc` |
| InsightSeeker | `/insights` response (summary + actionable insights + next deep dives) | (no tools) |

Implementation notes:
- The code now uses only the new agent identities above (no backward-compatible alias names).
- Chart creation/append workflow is centralized in `ChartManager`.
- Orchestrator policy requires tool-confirmed success before claiming chart creation:
  - `create_superset_chart` must return non-null `chart_id`
  - `append_chart_to_dashboard` must return `status` in `appended|already_exists`.

### 12.2 LLM Agent ↔ BI Tool (Superset)
- Request: The agent calls tools like `get_active_chart_data`, which cause FastAPI
  to call Superset `/api/v1/chart/data` using the latest log payload.
- Output: Superset returns chart data/metadata, which FastAPI returns to the agent.

### 12.3 LLM Agent ↔ Schema Explorer
- Request: The agent calls schema tools (`get_facts`, `get_schema_info`,
  `search_attribute`, `search_value_exists`).
- Output: Schema Explorer returns fact tables, dimensions, and attribute metadata
  from the local schema configuration.

### 12.4 FastAPI ↔ Database (DuckDB)
- Request: FastAPI writes chat logs, UI events, and Superset action logs.
- Output: DuckDB persists unified logs; FastAPI reads them for context and
  `/superset/logs/*` endpoints.

### 12.5 Superset ↔ DuckDB (indirect via FastAPI)
- Request: FastAPI poller queries Superset metadata DB for action logs.
- Output: Action log rows are written into DuckDB for unified analysis.

### 12.6 Cube ↔ DuckDB (analytics storage)
- Request: Cube receives SQL/REST queries (from Superset or FastAPI) and executes
  them against its internal DuckDB database (the Cube-managed warehouse).
- Output: Cube returns query results to the caller; the underlying DuckDB stores
  the analytics tables and is not queried directly by Superset or FastAPI.
