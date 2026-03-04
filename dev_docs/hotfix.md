# Hotfix: Missing `chart_click` / `legend_toggle` in `events.duckdb`

## Symptoms
- `superset_action_logs` contains UI-source events only as:
  - `bootstrap_init`
  - `iframe_loaded`
- No `action IN ('chart_click', 'legend_toggle')` rows are present.
- Most dashboard activity appears as Superset telemetry logs (for example `action='log'` with `event_name` values like `mount_dashboard`, `load_chart`, `select_dashboard_tab`).

## Why It Happened
- The current Streamlit run path is `session_iframe` mode (`streamlit-app/streamlit_app.py`).
- In this mode, rich dashboard interaction events are not reliably emitted via `postMessage` (explicitly noted in code comments).
- `chart_click` and `legend_toggle` are emitted by the React embed component path (`guest_token` mode) in:
  - `streamlit-app/superset_embed_component/frontend/src/SupersetEmbed.tsx`
- Since those events are never emitted, FastAPI `/events` cannot ingest them, so they do not appear in DuckDB.

## Current Ingestion Behavior (already correct)
- `/events` stores UI events in `superset_action_logs` with:
  - `action = event_type`
  - `json.source = 'ui'`
- Implemented in:
  - `fastapi/fastapi_service/main.py` (`POST /events`)
  - `fastapi/fastapi_service/writer.py` (`build_ui_payload`)

## Hotfix (Immediate)
- Session iframe remains the default and behaves as before (requires Superset login when refreshed). Use guest_token mode when you need embed-side event capture without relying on browser cookies.

Verification query:

```sql
SELECT action, COUNT(*) AS n
FROM superset_action_logs
WHERE json_extract_string(json, '$.source') = 'ui'
GROUP BY 1
ORDER BY n DESC;
```

Expected: `chart_click`, `legend_toggle`, filter events appear in both modes.

## UI event types captured by `/events`
- `cross_filter_added` / `cross_filter_removed` (cross-filter changes by slice)
- `global_filter_added` / `global_filter_removed` (native/global filters)
- `superset_tab_click` (tab switches)
- `superset_ui_event` family (generic payload relay when Superset posts `id='superset-ui-event'`; includes fields `event_type`, `chart_id`, `viz_type`, `payload`)
- Any custom `event` or `event_type` Superset `postMessage` with `id='superset-ui-event'` (passed through)
- Storage mapping: `action` in `superset_action_logs` equals the event type above (not a fixed `ui_event` bucket); `json.source = 'ui'`.

## Unified Schema Recommendation for `events.duckdb`
Use one canonical event table for both UI and Superset backend events. Keep existing `superset_action_logs` for compatibility, but normalize reads through a unified view/table.

### Canonical Event Shape
- `event_id` BIGINT
- `event_ts` TIMESTAMP
- `source` TEXT (`ui`, `superset_log`, `system`)
- `event_type` TEXT (normalized semantic type)
- `action_raw` TEXT (original action, e.g. `log`, `ChartDataRestApi.data`, `legend_toggle`)
- `session_id` TEXT
- `user_id` TEXT
- `device_id` TEXT
- `dashboard_id` BIGINT
- `slice_id` BIGINT
- `payload_json` JSON/TEXT
- `ingested_at` TIMESTAMP

### Normalization Rules
- UI rows:
  - `source='ui'`
  - `event_type = action`
  - `action_raw = action`
- Superset telemetry rows (`action='log'`):
  - `source='superset_log'`
  - `event_type = json.event_name` if present, else `log`
  - `action_raw = 'log'`
- Superset API/action rows:
  - `source='superset_log'`
  - `event_type = action` (e.g. `ChartDataRestApi.data`)
  - `action_raw = action`

### Practical Integration Path
1. Keep writing as-is to `superset_action_logs`.
2. Add a DB view `events_unified` that applies the above mappings.
3. Move dashboards/analytics/agent queries to `events_unified` instead of raw table logic.
4. Add a smoke test to assert `chart_click` + `legend_toggle` appear when in `guest_token` mode.

## Validation Queries
```sql
-- UI-only events
SELECT action, COUNT(*) n
FROM superset_action_logs
WHERE json_extract_string(json, '$.source') = 'ui'
GROUP BY 1
ORDER BY n DESC;

-- Check missing interaction events quickly
SELECT
  SUM(CASE WHEN action='chart_click' THEN 1 ELSE 0 END) AS chart_click_n,
  SUM(CASE WHEN action='legend_toggle' THEN 1 ELSE 0 END) AS legend_toggle_n
FROM superset_action_logs
WHERE json_extract_string(json, '$.source') = 'ui';
```

## Conclusion
- Missing `chart_click`/`legend_toggle` is a capture-path issue (embed mode), not a DuckDB write bug.
- Switching to `guest_token` enables those events.
- A unified schema/view should normalize UI + Superset logs into one consistent event contract for `events-[user_name].duckdb`.
