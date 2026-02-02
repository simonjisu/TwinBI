## FastAPI initial tests

The FastAPI initial implementation has unit tests under `unittest/`.

### Files

- `unittest/test_fastapi_service.py`
- `unittest/test_streamlit_fastapi_client.py`
- `unittest/test_superset_poller.py`

### Coverage

- `/health` returns status ok.
- `/events` responds ok and persists embed UI events into `superset_action_logs`.
- `/chat` persists a Streamlit chat log row into DuckDB and supports `debug: true`.
- `/poller/status` reports disabled state when Superset polling is not configured.
- `/poller/reset` clears the Superset checkpoint.
- `/poller/trigger` runs a single poll cycle (or returns disabled).
- `/poller/sync` appends missing Superset logs based on DuckDB max id and returns per-type counts.
- `/poller/clear` deletes Superset logs and resets the checkpoint.
- `/superset/users/lookup` returns 400 when Superset DB is not configured.
- `/superset/logs/latest` returns recent Superset logs from DuckDB.
- `/superset/logs/latest_sql` returns 404 when no chart data logs exist.
- `/events/stream` opens an SSE stream for Superset logs (including embed events).
- Superset `/datasource/samples` requests are labeled as `drill_to_details` in stream payloads.
- Drill-by apply logs (`event_name: "further_drill_by"`) are labeled as `drill_by` and include a `drill_by` payload in stream rows.
- `/superset/datasets/{dataset_id}/schema` returns 400 when Superset config is missing.
- `/semantic/meta` returns 400 when Cube REST URL is missing.
- `/semantic/schema` returns 400 when Cube conf path is missing.
- `/superset/dashboards/{dashboard_id}/charts` returns 400 when Superset config is missing.
- `/superset/charts/{chart_id}/data` returns 400 when Superset config is missing, or 404 when no chart log exists.
- `/chat/context/latest` returns a null context when no chat has been processed.
- `/chat/debug/latest` returns null until a debug chat is sent.
- `/chat/dialogue` returns a dialogue list for a session_id.
- `/superset/charts/{chart_id}/log-context` returns 404 when no logs exist.
- Streamlit FastAPI client builds URLs and posts to `/chat` and `/events`.
- Superset poller builds filtered SQL when dashboard/user filters are set.
- QueryTranslater parses Superset chart logs into SQL.
- QueryTranslater returns filters and where clauses from chart logs.
- QueryTranslater uses dataset schema metadata to qualify columns.
- QueryTranslater can infer cube table prefixes from Cube metadata when dataset schema is missing.
- Cube metadata schema summary builds table/column map from cube meta.
- Cube metadata join parsing extracts join keys from cube meta.
- Semantic metadata endpoint proxies `CUBE_REST_URL` `/meta`.
- Semantic metadata endpoint uses `CUBE_REST_URL/cubejs-api/v1/meta` with optional `CUBE_API_TOKEN`.
- Semantic schema endpoint parses `CUBE_CONF_PATH` YAML.

### Notes

- Tests create a temporary DuckDB file per test using `tempfile.TemporaryDirectory`.
- `TestClient` startup/shutdown hooks initialize the DuckDB writer; `flush_blocking` is called before asserting row counts.

### Run

```bash
python -m unittest discover -s unittest -p "test_fastapi_service.py"
python -m unittest discover -s unittest -p "test_streamlit_fastapi_client.py"
python -m unittest discover -s unittest -p "test_superset_poller.py"
python -m unittest discover -s unittest -p "test_superset_query_translater.py"
```

### Note

The `unittest/` directory name conflicts with Python's standard library package name.
Use discovery (as above) or run each file directly (e.g. `python unittest/test_fastapi_service.py`).

### Poller sync response shape

```python
sync = client.post("/poller/sync")
self.assertEqual(sync.status_code, 200)
self.assertIn(sync.json()["status"], {"ok", "disabled"})
inserted = sync.json().get("inserted", {})
self.assertIn("logs", inserted)
self.assertIn("chat", inserted)
```

### UI events persistence test

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

### Chat endpoint test (with history)

```python
payload = {
    "session_id": "s_2",
    "user_id": "u_2",
    "message": "hello",
    "history": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "how can I help?"},
    ],
    "active_chart_id": 316,
    "active_chart_name": "Sales by Product",
    "debug": True,
}
response = client.post("/chat", json=payload)
self.assertEqual(response.status_code, 200)
self.assertIn("debug", response.json())
```

### Superset logs latest endpoint test

```python
latest = client.get("/superset/logs/latest")
self.assertEqual(latest.status_code, 200)
self.assertIsInstance(latest.json(), list)
```

### Superset latest SQL endpoint test

```python
latest_sql = client.get("/superset/logs/latest_sql")
self.assertEqual(latest_sql.status_code, 404)
```

### Superset dataset schema endpoint test

```python
schema = client.get("/superset/datasets/12/schema")
self.assertEqual(schema.status_code, 400)
```

### Superset chart data endpoint test

```python
data = client.get("/superset/charts/12/data")
self.assertEqual(data.status_code, 400)
payload = data.json()
self.assertIn("detail", payload)
```

### Superset chart log context endpoint test

```python
log_context = client.get("/superset/charts/12/log-context")
self.assertEqual(log_context.status_code, 404)
```

### Chat context latest endpoint test

```python
context = client.get("/chat/context/latest")
self.assertEqual(context.status_code, 200)
self.assertIsNone(context.json().get("context"))
```

### Chat debug latest endpoint test

```python
debug = client.get("/chat/debug/latest")
self.assertEqual(debug.status_code, 200)
self.assertIsNone(debug.json().get("debug"))
```

### Chat dialogue endpoint test

```python
dialogue = client.get("/chat/dialogue", params={"session_id": "s_1"})
self.assertEqual(dialogue.status_code, 200)
self.assertIn("dialogue", dialogue.json())
```

### Cube meta endpoint test

```python
cube_meta = client.get("/semantic/meta")
self.assertEqual(cube_meta.status_code, 400)
```

### Cube schema endpoint test

```python
cube_schema = client.get("/semantic/schema")
self.assertEqual(cube_schema.status_code, 400)
```

### Superset query translator test

```python
parsed = QueryTranslater().parse_payload(json.dumps(payload))
self.assertIsNotNone(parsed)
self.assertIn("SUM(total_receipts)", parsed["sql"])
```

### Cube schema summary test

```python
summary = summarize_schema(meta)
self.assertIn("dim_product", summary)
```

### Cube join map test

```python
joins = extract_join_map(meta)
self.assertIn("fact_sales", joins)
```

### Superset logs stream endpoint test

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

### Drill-by log decoration test (example)

```python
row = {
    "action": "log",
    "json": json.dumps(
        {
            "event_name": "further_drill_by",
            "slice_id": 314,
            "drill_depth": 2,
            "drill_column": "dim_product_brand",
            "drill_filters": [{"col": "dim_product_brand", "op": "IN", "val": ["Adidas"]}],
        }
    ),
}
decorated = _decorate_superset_log(row)
self.assertEqual(decorated["action_label"], "drill_by")
self.assertIn("drill_by", decorated)
```

### UI events stream endpoint test

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

### FastAPI dashboard charts endpoint test

```python
def test_superset_dashboard_charts_missing_config(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = self._build_settings(str(Path(tmpdir) / "events.duckdb"))
        app = create_app(settings)
        with TestClient(app) as client:
            response = client.get("/superset/dashboards/12/charts")
            self.assertEqual(response.status_code, 400)
```
