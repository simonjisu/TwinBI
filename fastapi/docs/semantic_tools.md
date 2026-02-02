Semantic Tools

Overview
These tools use the semantic layer (Cube REST API + repo config) to list cubes/views,
inspect schema, create views, and query data. Use them when the user asks for data
directly from Cube or needs Cube member names.

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

- create_cube_view
  Creates or updates a semantic view in Cube config.
  Input: view_json (str) - JSON string matching ViewSpec.
  Output: {"status": "created|updated", "view_name": "...", "view_file": "...",
           "cube_reload_status": "ok|skipped|error", "physical_name": "...", "warnings": [...]}

- sync_superset_dataset
  Creates or refreshes a Superset dataset for a view/table.
  Input: request_json (str) - JSON string matching SupersetDatasetSyncRequest.
  Output: {"status": "created|updated", "dataset_id": int|None, "created": bool,
           "updated": bool, "warnings": [...]}

- create_view_and_sync
  Convenience wrapper: create a view then sync a Superset dataset.
  Input: view_json (str) - ViewSpec JSON (must include superset_sync fields).
  Output: {"view_result": {...}, "superset_result": {...|None}}

Example query_json
{"measures":["sales.total_sales"],"dimensions":["sales.brand"],"limit":10}

Typical usage patterns
- "What cubes/views are available?" -> list_cube_tables
- "What fields exist in sales?" -> get_cube_schema("sales")
- "Run a Cube query for total sales by brand" -> query_cube(query_json)
- "Create a new semantic view" -> create_cube_view(view_json) then sync_superset_dataset(request_json)

Notes
- Requires CUBE_REST_URL for live queries and CUBE_CONF_PATH for repo schema.
- The query_json must be valid JSON.
