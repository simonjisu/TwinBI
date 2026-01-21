Cube Tools

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
