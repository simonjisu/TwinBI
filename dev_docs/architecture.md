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
- Network: `agent4olap_net` (external)

**Implication:** Any container on `agent4olap_net` can query Cube via:
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

Add a new service (e.g., `api`) to the same network (`agent4olap_net`) to act as:

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
2. Streamlit calls `POST /chat` on FastAPI with `{session_id, user_id, message}`
3. FastAPI:
   - returns a stub response (LLM/Cube integrations are pending)
4. FastAPI logs:
   - Streamlit chat payload (session_id, request_id, message, response, latency_ms)

### 4.2 Streamlit chat logging flow
1. User submits chat in Streamlit
2. Streamlit emits `POST /chat` with:
   - `session_id`, `user_id`, `message`
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

Integration points:
- `FASTAPI_INTERNAL_URL=http://api:8000` (recommended new env var)
- Existing Superset URLs remain unchanged

---

### 5.2 FastAPI (Backend Orchestration + Logging)
Responsibilities:
- `/chat`: Streamlit chat logging + (future) LLM/Cube orchestration
- `/events`: reserved (no persistence by default)
- background: Superset log poller
- background: DuckDB writer

Suggested internal modules:
- `llm/` (provider adapters, prompt templates, tool calling)
- `cube/` (REST + SQL clients)
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

**Response**

```
{"status":"ok"}
```

**Note**: `POST /events` writes UI events (e.g., tab clicks) into DuckDB `ui_events`.

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

### GET /superset/dashboards/{dashboard_id}/charts

Returns the list of charts (figures) for a Superset dashboard.

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
      "datasource_type": "table"
    }
  ]
}
```

**Unit test (example)**

```python
def test_superset_dashboard_charts_missing_config(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = self._build_settings(str(Path(tmpdir) / "events.duckdb"))
        app = create_app(settings)
        with TestClient(app) as client:
            response = client.get("/superset/dashboards/12/charts")
            self.assertEqual(response.status_code, 400)
```

---

### GET /superset/dashboards/{dashboard_id}/tab-map

Returns a mapping of chart (slice) ids to Superset dashboard tab names.

**Response (example)**

```json
{
  "dashboard_id": 12,
  "tab_map": {
    "160": "Q1 Overview",
    "161": "Q1 Overview"
  }
}
```

**Unit test (example)**

```python
tab_map = client.get("/superset/dashboards/12/tab-map")
self.assertEqual(tab_map.status_code, 400)
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

### GET /cube/meta

Returns Cube.js metadata from the Cube REST API.

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
cube_meta = client.get("/cube/meta")
self.assertEqual(cube_meta.status_code, 400)
```

---

### GET /cube/schema

Returns a schema/joins map built from Cube config files under `CUBE_CONF_PATH`.

**Response (example)**

```json
{
  "fact_sales": {
    "sql_table": "main.fact_sales",
    "columns": ["sale_id", "total_receipts"],
    "joined": {
      "dim_date": {
        "relationship": "many_to_one",
        "sql": "{CUBE}.date_key = {dim_date}.date_key",
        "joined_key": [
          {
            "from": "fact_sales.date_key",
            "to": "dim_date.date_key"
          }
        ]
      }
    }
  }
}
```

**Unit test (example)**

```python
cube_schema = client.get("/cube/schema")
self.assertEqual(cube_schema.status_code, 400)
```

---
### GET /superset/logs/stream

Streams Superset action logs from DuckDB using Server-Sent Events (SSE).

**Query params**

- `dashboard_id`: filter by dashboard id
- `user_id`: filter by Superset user id
- `action`: filter by action name
- `last_id`: start from this superset_log_id (default 0)
- `limit`: max rows per poll (default 100)
- `poll_interval_sec`: poll interval (default 1.0)
- `Last-Event-ID` header: optional resume token used on reconnects

**Notes**

- When `action` is `ChartDataRestApi.data` or `ChartDataRestApi.json_dumps`, the stream payload includes `translated_sql`.
- The stream payload also includes `translated_filters` (list of filter dicts) and `translated_where` (rendered WHERE clauses).
- SQL translation uses dataset metadata (table name + columns) when available.
- When dataset metadata is missing, SQL translation can infer table/column prefixes from Cube metadata (`CUBE_REST_URL` `/cubejs-api/v1/meta`) using `aliasMember` + cube joins.
- SQL translation uses Cube join metadata (join sql) to render JOIN clauses when available, and falls back to `CUBE_CONF_PATH` schema/joins.
- Cube metadata is fetched from `CUBE_REST_URL` (`/cubejs-api/v1/meta`); optional `CUBE_API_TOKEN` is sent as `Authorization`.

**Unit test (example)**

```python
with client.stream(
    "GET",
    "/superset/logs/stream",
    headers={"Last-Event-ID": "0"},
) as stream:
    self.assertEqual(stream.status_code, 200)
    content_type = stream.headers.get("content-type", "")
    self.assertTrue(content_type.startswith("text/event-stream"))
```

---

### GET /events/stream

Streams UI events (from `ui_events`) using Server-Sent Events (SSE).

**Query params**

- `session_id`: filter by Streamlit session id
- `event_type`: filter by event type
- `last_id`: start from this event_id (default 0)
- `limit`: max rows per poll (default 100)
- `poll_interval_sec`: poll interval (default 1.0)
- `Last-Event-ID` header: optional resume token used on reconnects

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

### GET /superset/users/lookup

Resolves a Superset username to user id (metadata DB lookup).

## 7. Logging & Correlation Strategy

### 7.1 Common identifiers

- **session_id**: stable across a user session in Streamlit  
- **request_id**: unique per `/chat` call  
- **event_id**: unique per event row (generated at ingestion time)  
- **superset_log_id**: Superset `logs.id` (source primary key)

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

#### ui_events

- `event_id` BIGINT  
- `ts` TIMESTAMP  
- `session_id` VARCHAR  
- `user_id` VARCHAR  
- `event_type` VARCHAR  
- `payload_json` VARCHAR  

#### superset_action_logs

(ingested from `superset_db.logs`; keep raw + add ingestion columns)

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

- same external network: `agent4olap_net`

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
Streamlit -> FastAPI (/chat): session_id, message
FastAPI -> DuckDB: write streamlit_chat_logs
FastAPI -> Streamlit: answer (stub)
```

### 10.2 Superset action log ingestion

```
FastAPI Poller -> superset_db: SELECT logs WHERE id > last_id
superset_db -> FastAPI Poller: rows
FastAPI Writer -> DuckDB: append superset_action_logs
FastAPI Writer -> DuckDB: update checkpoint

---

## 11. Superset Interactive Event Logging Plan

Goal: capture richer, user-level dashboard interactions beyond basic REST calls.

### 11.0 Event Logs DuckDB schema and data dictionary

All event logs are stored in `events.duckdb` (mounted at `./data/logs/events.duckdb`).

#### Table: streamlit_chat_logs
- `ts` (TIMESTAMP): FastAPI receipt time (UTC).
- `session_id` (VARCHAR): Streamlit session identifier.
- `request_id` (VARCHAR): Unique id per chat request.
- `user_id` (VARCHAR): Streamlit user id string (if provided).
- `message` (VARCHAR): User message text.
- `response` (VARCHAR): Assistant response text.
- `latency_ms` (BIGINT): API latency for `/chat`.

#### Table: superset_action_logs
Raw Superset action log rows ingested from `superset_db.logs`.
- `superset_log_id` (BIGINT): Superset `logs.id` primary key.
- `dttm` (TIMESTAMP): Superset action timestamp (`logs.dttm`).
- `action` (VARCHAR): Action name (e.g., `DashboardRestApi.get`, `log`, `ChartDataRestApi.data`).
- `user_id` (BIGINT): Superset user id (`logs.user_id`).
- `dashboard_id` (BIGINT): Superset dashboard id.
- `slice_id` (BIGINT): Superset slice (chart) id.
- `duration_ms` (BIGINT): Action duration in ms.
- `referrer` (VARCHAR): Referrer URL (if present).
- `json` (VARCHAR): Raw JSON payload from Superset logs.
- `ingested_at` (TIMESTAMP): FastAPI ingestion timestamp (UTC).

#### Table: _checkpoint
- `key` (VARCHAR, PK): Checkpoint name.
- `value` (VARCHAR): Checkpoint value (e.g., last ingested Superset log id).

### 11.0.1 Superset logs source table (metadata DB)

Superset writes action logs to the metadata DB table `logs`.
Key columns:
- `id` (BIGINT): Primary key.
- `dttm` (TIMESTAMP): Event time.
- `action` (VARCHAR): Action name.
- `user_id` (BIGINT): Superset user id.
- `dashboard_id` (BIGINT): Dashboard id (if applicable).
- `slice_id` (BIGINT): Chart id (if applicable).
- `duration_ms` (BIGINT): Timing metric for the action.
- `referrer` (VARCHAR): Referrer URL.
- `json` (TEXT): JSON payload (for `action='log'` and others).

### 11.1 Current limits
- Superset Action Log entries are mostly server-side endpoints (e.g., `DashboardRestApi.get`).
- Interactive client events are often stored as `action='log'` with JSON payloads.
- Embedded dashboards may emit `/superset/log/?explode=events` requests that batch UI events.

### 11.2 Proposed plan
1) **Inventory actual actions**
   - Run `SELECT action, count(*) FROM logs GROUP BY 1 ORDER BY 2 DESC;`
   - Identify actions tied to dashboards (e.g., `ChartRestApi.data`, `explore_json`, `log`)
2) **Parse `action='log'` payloads (future)**
   - Extract `event_name`, `dashboard_id`, `slice_id`, filter metadata, and timing from JSON
   - Store parsed fields in a new table (e.g., `superset_interaction_events`)
3) **Add optional client event ingestion (future)**
   - If `action='log'` is insufficient, enable Superset event logging configuration
   - Capture the batched event payloads for embedded dashboards
4) **Define a taxonomy**
   - Map raw event names to UX categories: filter change, drill, cross-filter, export, refresh, etc.
5) **Backfill strategy**
   - Reset checkpoint and re-ingest after parser is in place
   - Keep raw logs alongside parsed events for auditability

### 11.3 Implementation hooks (future)
- Extend the poller to detect `action='log'` and parse JSON into structured columns.
- Add a new DuckDB table for parsed interactions if needed.
- Keep `superset_action_logs` as raw source-of-truth.

References:
- Superset event logging docs: https://superset.apache.org/docs/configuration/event-logging/
- HomeToGo logging analysis: https://engineering.hometogo.com/monitor-superset-usage-via-superset-c7f9fba79525?gi=294843d271e9
```
