from __future__ import annotations
import duckdb
import asyncio
import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from fastapi_service.superset import (
    fetch_chart_data_from_log,
    fetch_dashboard_charts,
)
from fastapi_service.cube import fetch_cube_meta, run_cube_query
from fastapi_service.cube_conf import load_repo_schema
# import sys
# proj_path = Path(__file__).resolve().parent.parent
# sys.path.append(str(proj_path))
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _PROJECT_ROOT / "src"
if _SRC_ROOT.exists():
    sys.path.insert(0, str(_SRC_ROOT))
if _PROJECT_ROOT.exists():
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from src.schema_processor import SchemaExplorer
except Exception as exc:  # pragma: no cover - optional dependency
    SchemaExplorer = None
    _SCHEMA_IMPORT_ERROR = exc
else:
    _SCHEMA_IMPORT_ERROR = None

try:
    from agents import Agent, ModelSettings, Runner, function_tool, RunItemStreamEvent
except Exception as exc:  # pragma: no cover - optional dependency
    Agent = None
    ModelSettings = None
    Runner = None
    function_tool = None
    RunItemStreamEvent = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

class Answer(BaseModel):
    answer: dict[str, Any] = Field(..., description="Final answer.")


@dataclass
class AgentContext:
    settings: Any
    conn: Any
    chart_id: int | None = None
    chart_name: str | None = None
    chart_data: dict[str, Any] | None = None


_ACTIVE_CONTEXT: AgentContext | None = None


def _get_active_context() -> AgentContext | None:
    return _ACTIVE_CONTEXT


def _get_active_settings() -> tuple[Any | None, dict[str, Any] | None]:
    context = _get_active_context()
    if context is None:
        return None, {"error": "active context not set"}
    if not context.settings:
        return None, {"error": "settings not available"}
    return context.settings, None


def _read_doc_file(path: Path) -> str | dict[str, Any]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"error": f"doc not found: {path}"}
    except Exception as exc:
        return {"error": f"failed to read doc: {exc}"}


def _fetch_latest_chart_log_payload(
    conn: duckdb.DuckDBPyConnection,
    chart_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT json
        FROM superset_action_logs
        WHERE slice_id = ? AND action IN ('ChartDataRestApi.data', 'ChartDataRestApi.json_dumps')
        ORDER BY superset_log_id DESC
        LIMIT 1
        """,
        [chart_id],
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def _fetch_latest_dashboard_id(conn: duckdb.DuckDBPyConnection) -> int | None:
    row = conn.execute(
        """
        SELECT dashboard_id
        FROM superset_action_logs
        WHERE dashboard_id IS NOT NULL
        ORDER BY superset_log_id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return row[0]


if function_tool:
    _SCHEMA_EXPLORER_ERROR: Exception | None = None
    _SCHEMA_EXPLORER: SchemaExplorer | None = None
    if SchemaExplorer is None:
        _SCHEMA_EXPLORER_ERROR = _SCHEMA_IMPORT_ERROR
    else:
        try:
            _SCHEMA_EXPLORER = SchemaExplorer(Path("./data/sales"), schema_type="star")
        except Exception as exc:  # pragma: no cover - optional data dependency
            _SCHEMA_EXPLORER_ERROR = exc

    def _schema_explorer_or_error() -> tuple[SchemaExplorer | None, dict[str, Any] | None]:
        if _SCHEMA_EXPLORER is not None:
            return _SCHEMA_EXPLORER, None
        if _SCHEMA_EXPLORER_ERROR:
            return None, {"error": f"schema explorer not available: {_SCHEMA_EXPLORER_ERROR}"}
        return None, {"error": "schema explorer not available"}

    @function_tool
    def get_active_chart_log() -> dict[str, Any]:
        """
        Return the latest Superset log payload for the active chart.

        Input:
        - Uses the active context set by the API handler (chart id + DuckDB connection).

        Output:
        - {"chart_id": int, "payload": dict | null}
        - {"error": "..."} on missing chart id or missing payload.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if not context.chart_id:
            return {"error": "active chart id not set"}
        payload = _fetch_latest_chart_log_payload(context.conn, context.chart_id)
        return {"chart_id": context.chart_id, "payload": payload}

    @function_tool
    def get_active_chart_data() -> dict[str, Any]:
        """
        Fetch chart data from Superset using the latest log payload.

        Input:
        - Uses the active context set by the API handler (chart id + settings).

        Output:
        - {"chart_id": int, "data": dict} (raw Superset /api/v1/chart/data response)
        - {"error": "..."} on missing chart id or missing payload.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if not context.chart_id:
            return {"error": "active chart id not set"}
        payload = _fetch_latest_chart_log_payload(context.conn, context.chart_id)
        if not payload:
            return {"error": "no chart log payload found"}
        data = fetch_chart_data_from_log(context.settings, payload)
        return {"chart_id": context.chart_id, "data": data['raw']}

    @function_tool
    def get_chart_sql() -> dict[str, Any]:
        """
        Return the latest SQL/query for the active chart from DuckDB logs.

        Input:
        - Uses the active context set by the API handler (chart id + DuckDB connection).

        Output:
        - {"chart_id": int, "sql": str | null}
        - {"error": "..."} if no log payload or invalid JSON.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if not context.chart_id:
            return {"error": "active chart id not set"}
        row = context.conn.execute(
            """
            SELECT json
            FROM superset_action_logs
            WHERE slice_id = ? AND action IN ('ChartDataRestApi.data', 'ChartDataRestApi.json_dumps')
            ORDER BY superset_log_id DESC
            LIMIT 1
            """,
            [context.chart_id],
        ).fetchone()
        if not row or not row[0]:
            return {"error": "no chart log payload found"}
        try:
            payload = json.loads(row[0])
        except json.JSONDecodeError:
            return {"error": "log payload is not valid JSON"}
        return {"chart_id": context.chart_id, "sql": payload.get("sql") or payload.get("query")}

    @function_tool
    def get_chart_metadata() -> dict[str, Any]:
        """
        Return chart metadata for the active chart via Superset API.

        Input:
        - Uses the active context set by the API handler (chart id + settings).

        Output:
        - {"chart_id": int, "metadata": dict | null}
        - {"error": "..."} if dashboard id is missing or chart not found.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        if not context.chart_id:
            return {"error": "active chart id not set"}
        payload = _fetch_latest_chart_log_payload(context.conn, context.chart_id)
        dashboard_id = payload.get("dashboard_id") if isinstance(payload, dict) else None
        if not dashboard_id:
            return {"error": "dashboard id not found in log payload"}
        charts = fetch_dashboard_charts(context.settings, int(dashboard_id))
        for chart in charts:
            if str(chart.get("slice_id")) == str(context.chart_id):
                return {"chart_id": context.chart_id, "metadata": chart}
        return {"chart_id": context.chart_id, "metadata": None}

    @function_tool
    def list_dashboard_charts() -> dict[str, Any]:
        """
        List charts for the most relevant dashboard.

        Input:
        - Uses the active context to resolve the dashboard id:
          1) settings.superset_log_dashboard_id if configured
          2) latest dashboard id from DuckDB logs

        Output:
        - {"dashboard_id": int, "charts": [..]}
        - {"error": "..."} if no dashboard id is available.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        dashboard_id = context.settings.superset_log_dashboard_id
        if not dashboard_id:
            dashboard_id = _fetch_latest_dashboard_id(context.conn)
        if not dashboard_id:
            return {"error": "dashboard id not available"}
        charts = fetch_dashboard_charts(context.settings, int(dashboard_id))
        return {"dashboard_id": int(dashboard_id), "charts": charts}

    @function_tool
    def get_chart_data_by_id(chart_id: int) -> dict[str, Any]:
        """
        Fetch chart data for a specific chart id using the latest log payload.

        Input:
        - chart_id: Superset slice id (chart id).

        Output:
        - {"chart_id": int, "data": dict} raw Superset chart data response
        - {"error": "..."} if no log payload is found.
        """
        context = _get_active_context()
        if context is None:
            return {"error": "active context not set"}
        payload = _fetch_latest_chart_log_payload(context.conn, chart_id)
        if not payload:
            return {"error": "no chart log payload found"}
        data = fetch_chart_data_from_log(context.settings, payload)
        return {"chart_id": chart_id, "data": data}

    @function_tool
    def read_dashboard_tools_doc() -> str | dict[str, Any]:
        """Return the dashboard tools documentation markdown."""
        return _read_doc_file(Path("fastapi/docs/dashboard_tools.md"))

    @function_tool
    def read_schema_explorer_doc() -> str | dict[str, Any]:
        """Return the schema explorer documentation markdown."""
        return _read_doc_file(Path("fastapi/docs/schema_explorer.md"))

    @function_tool
    def read_cube_tools_doc() -> str | dict[str, Any]:
        """Return the Cube tools documentation markdown."""
        return _read_doc_file(Path("fastapi/docs/cube_tools.md"))

    @function_tool
    def list_cube_tables() -> list[dict[str, Any]] | dict[str, Any]:
        """
        List cubes and views available from Cube configuration or meta.

        Output:
        - [{"name": "...", "type": "cube|view"}]
        """
        settings, error = _get_active_settings()
        if error:
            return error

        entries: dict[str, dict[str, Any]] = {}
        if settings.cube_conf_path:
            schema = load_repo_schema(settings.cube_conf_path)
            for name, info in schema.items():
                entry_type = info.get("type") or "cube"
                entries[name] = {"name": name, "type": entry_type}

        if settings.cube_rest_url:
            meta = fetch_cube_meta(settings)
            for cube in meta.get("cubes", []) or []:
                if isinstance(cube, dict) and cube.get("name"):
                    name = cube["name"]
                    entries.setdefault(name, {"name": name, "type": "cube"})

        if not entries:
            return {"error": "cube configuration and meta not available"}
        return sorted(entries.values(), key=lambda item: (item.get("type"), item.get("name")))

    @function_tool
    def get_cube_schema(table_name: str) -> dict[str, Any]:
        """
        Return schema details for a cube or view.

        Output (conf-based):
        - {"name": "...", "type": "cube|view", "columns": [...], "sql_table": "...", "joined": {...}}

        Output (meta-based cube):
        - {"name": "...", "type": "cube", "measures": [...], "dimensions": [...], "segments": [...]}
        """
        settings, error = _get_active_settings()
        if error:
            return error

        if settings.cube_conf_path:
            schema = load_repo_schema(settings.cube_conf_path)
            entry = schema.get(table_name)
            if entry:
                entry_type = entry.get("type") or "cube"
                return {"name": table_name, "type": entry_type, **entry}

        if settings.cube_rest_url:
            meta = fetch_cube_meta(settings)
            for cube in meta.get("cubes", []) or []:
                if isinstance(cube, dict) and cube.get("name") == table_name:
                    measures = [m.get("name") for m in (cube.get("measures") or []) if m.get("name")]
                    dimensions = [d.get("name") for d in (cube.get("dimensions") or []) if d.get("name")]
                    segments = [s.get("name") for s in (cube.get("segments") or []) if s.get("name")]
                    joins = list((cube.get("joins") or {}).keys()) if isinstance(cube.get("joins"), dict) else []
                    return {
                        "name": table_name,
                        "type": "cube",
                        "measures": measures,
                        "dimensions": dimensions,
                        "segments": segments,
                        "joins": joins,
                    }

        return {"error": f"cube or view '{table_name}' not found"}

    @function_tool
    def query_cube(query_json: str) -> dict[str, Any]:
        """
        Run a Cube REST API query (/cubejs-api/v1/load).

        Input:
        - query_json: JSON string for a Cube query object (or list for data blending).

        Output:
        - Raw Cube response payload.
        """
        settings, error = _get_active_settings()
        if error:
            return error
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            return {"error": f"query_json is not valid JSON: {exc}"}
        if not isinstance(query, (dict, list)):
            return {"error": "query_json must be a JSON object or list"}
        try:
            return run_cube_query(settings, query)
        except Exception as exc:
            return {"error": f"cube query failed: {exc}"}

    @function_tool
    def get_facts() -> list[str] | dict[str, Any]:
        """
        Return the list of fact tables in the configured star schema.

        Example:
        - ["fact_sales"]
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.get_facts()

    @function_tool
    def get_schema_info(fact_table: str) -> list[dict] | dict[str, Any]:
        """
        Return the schema graph nodes for a fact table.

        Each node includes `name` and `type` ("fact" or "dimension").
        Dimension nodes include `attributes`; the fact node includes `measures` and `fks`.

        Example:
        - [{"name": "fact_sales", "type": "fact", "measures": [...], "fks": [...]},
           {"name": "dim_date", "type": "dimension", "attributes": [...]}, ...]
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.get_schema(fact_table)

    @function_tool
    def search_attribute(fact_table: str, attribute_name: str) -> list[dict] | dict[str, Any]:
        """
        Return hierarchy paths that lead to the attribute for the fact schema.

        Each result includes: dimension, attribute, path (dimension/level/attribute steps),
        and stats (count, distinct_count, min/max, dtype, unique_values metadata).
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.search_attribute(fact_table, attribute_name)

    @function_tool
    def search_value_exists(
        fact_table: str,
        attribute_name: str,
        value: str,
    ) -> bool | dict[str, Any]:
        """
        Return whether the attribute value exists for the given fact table and attribute.

        Example:
        - search_value_exists("fact_sales", "year", "2024") -> True
        """
        explorer, error = _schema_explorer_or_error()
        if error:
            return error
        return explorer.search_value(fact_table, attribute_name, value)

class AgentRunner:
    def __init__(self) -> None:
        self._router_agent = self._build_agents()

    @property
    def available(self) -> bool:
        return self._router_agent is not None

    def _build_agents(self) -> Any:
        if Agent is None or ModelSettings is None:
            return None

        model_settings = ModelSettings(
            reasoning={"effort": "medium"},
            verbosity="low",
            max_turns=50,
            response_format={"type": "json_object", "schema": Answer.model_json_schema()},
        )
        return Agent(
            name="Dashboard Agent",
            model=os.getenv("AGENT_MODEL", "gpt-5-nano"),
            tools=[
                get_active_chart_log,
                get_active_chart_data,
                get_chart_sql,
                get_chart_metadata,
                list_dashboard_charts,
                get_chart_data_by_id,
                read_dashboard_tools_doc,
                read_schema_explorer_doc,
                read_cube_tools_doc,
                list_cube_tables,
                get_cube_schema,
                query_cube,
                get_facts,
                get_schema_info,
                search_attribute,
                search_value_exists,
            ]
            if function_tool
            else [],
            model_settings=model_settings,
            instructions=(
                "[SYSTEM DATE] December 31st, 2024. "
                "You answer questions about the current dashboard and charts. "
                "Use the provided chart context when available. "
                "When an active chart is available, fetch the active chart data and "
                "call `list_dashboard_charts` to see which charts will be helpful "
                "Then use `get_chart_data_by_id` to fetch data "
                "to explore the chart to give better answers "
                "If no active chart is available, explore dashboards by listing charts "
                "and fetching relevant chart data as needed. "
                "Use the schema explorer tools when the user asks about available "
                "tables, measures, dimensions, or values; or when you need to map "
                "a natural-language request to schema fields. "
                "Use the Cube tools to list cubes/views, inspect Cube schema, or "
                "run Cube queries when the user asks for data directly from Cube. "
                "You may read documentation via tools; "
                "Return a JSON object with an 'answer' field containing the response."
            ),
        )

    async def respond(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
        context_obj: Any | None = None,
        debug: bool = False,
    ) -> tuple[str, list[dict[str, Any]]]:
        if not self._router_agent:
            return (
                "Agent not available. "
                "Install the OpenAI agents package and set OPENAI_API_KEY."
            ), [{"type": "error", "message": "agent_not_available"}] if debug else []

        prompt = self._format_prompt(message, history, context=context)
        debug_items: list[dict[str, Any]] = []
        try:
            global _ACTIVE_CONTEXT
            _ACTIVE_CONTEXT = context_obj
            result = await self._run_agent(self._router_agent, prompt, context_obj)
        except Exception as exc:
            if debug:
                debug_items.append({"type": "error", "message": str(exc)})
            return "Agent call failed. Check API credentials and logs.", debug_items
        finally:
            _ACTIVE_CONTEXT = None
        answer = self._extract_answer(result)
        if debug:
            debug_items.extend(self._extract_debug_items(result))
        return answer, debug_items

    async def respond_stream(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
        context_obj: Any | None = None,
        debug: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self._router_agent:
            yield {"event": "error", "message": "agent_not_available"}
            return

        prompt = self._format_prompt(message, history, context=context)
        last_text = ""
        try:
            global _ACTIVE_CONTEXT
            _ACTIVE_CONTEXT = context_obj
            stream = Runner.run_streamed(self._router_agent, prompt, context=context_obj)
            async for event in stream.stream_events():
                if RunItemStreamEvent and isinstance(event, RunItemStreamEvent):
                    name = event.name
                    item = event.item
                    if name == "reasoning_item_created":
                        yield {"event": "reasoning", "detail": "Reasoning ..."}
                    elif name == "tool_called":
                        tool_name = getattr(item.raw_item, "name", None)
                        tool_args = getattr(item.raw_item, "arguments", None)
                        yield {
                            "event": "tool_called",
                            "tool": tool_name,
                            "arguments": tool_args,
                        }
                    elif name == "tool_output":
                        output = getattr(item, "output", None)
                        yield {"event": "tool_output", "output": output}
                    elif name in ("message_output_created", "message_output_updated"):
                        raw_item = getattr(item, "raw_item", None)
                        text = None
                        if isinstance(raw_item, dict):
                            content = raw_item.get("content")
                            if isinstance(content, list) and content:
                                text = content[0].get("text")
                        if text is None:
                            text = getattr(item, "output_text", None)
                        if isinstance(text, str):
                            last_text = text
                            yield {"event": "message", "text": text}
        except Exception as exc:
            yield {"event": "error", "message": str(exc)}
            return
        finally:
            _ACTIVE_CONTEXT = None

        final_text = getattr(stream, "final_output", None) or last_text
        answer = self._parse_json_answer(final_text) if isinstance(final_text, str) else str(final_text)
        yield {"event": "final", "answer": answer}

    def _format_prompt(
        self,
        message: str,
        history: list[dict[str, str]],
        context: str | None = None,
    ) -> str:
        lines = []
        if context:
            lines.append("context: " + context)
        for item in history[-20:]:
            role = (item.get("role") or "").strip()
            content = (item.get("content") or "").strip()
            if not role or not content:
                continue
            lines.append(f"{role}: {content}")
        if not (
            history
            and (history[-1].get("role") or "").strip() == "user"
            and (history[-1].get("content") or "").strip() == message.strip()
        ):
            lines.append(f"user: {message}")
        return "\n".join(lines)

    async def _run_agent(self, agent: Any, prompt: str, context_obj: Any | None) -> Any:
        if Runner is None:
            raise RuntimeError(_IMPORT_ERROR or "agents Runner unavailable")

        run_sync = getattr(Runner, "run_sync", None)
        if callable(run_sync):
            return await asyncio.to_thread(run_sync, agent, prompt, context=context_obj)

        run_async = getattr(Runner, "run", None)
        if callable(run_async):
            result = run_async(agent, prompt, context=context_obj)
            if asyncio.iscoroutine(result):
                return await result
            return result

        raise RuntimeError("agents Runner has no run method")

    def _extract_answer(self, result: Any) -> str:
        if isinstance(result, str):
            return self._parse_json_answer(result)

        for attr in ("final_output", "output_text", "output"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return self._parse_json_answer(value)
        if isinstance(result, dict):
            return self._parse_json_answer(json.dumps(result))
        return str(result)

    def _parse_json_answer(self, text: str) -> str:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(payload, dict):
            answer = payload.get("answer") or payload.get("message") or payload.get("text")
            if isinstance(answer, str) and answer.strip():
                return answer
        return text

    def _extract_debug_items(self, result: Any) -> list[dict[str, Any]]:
        items = []
        new_items = getattr(result, "new_items", None)
        if not isinstance(new_items, list):
            final_text = self._extract_answer(result)
            if final_text:
                items.append({"type": "final_output", "final_output": final_text})
            return items
        doc_tools = {
            "read_dashboard_tools_doc": "dashboard tools",
            "read_schema_explorer_doc": "schema explorer",
            "read_cube_tools_doc": "cube tools",
        }
        for item in new_items:
            item_type = getattr(item, "type", None) or "unknown_item"
            if item_type == "reasoning_item":
                items.append({"type": item_type, "detail": "Reasoning ..."})
                continue
            raw = getattr(item, "raw_item", None)
            if item_type == "tool_call_item":
                name = getattr(raw, "name", None)
                if name is None and isinstance(raw, dict):
                    name = raw.get("name")
                if name in doc_tools:
                    items.append(
                        {
                            "type": item_type,
                            "name": f"reading {doc_tools[name]} document...",
                        }
                    )
                else:
                    items.append({"type": item_type, "name": name or "unknown_tool"})
                continue
            if item_type == "tool_call_output_item":
                output = None
                if isinstance(raw, dict):
                    output = raw.get("output")
                name = None
                if isinstance(raw, dict):
                    name = raw.get("name") or raw.get("tool_name")
                if name in doc_tools:
                    items.append({"type": item_type, "output": "documentation loaded"})
                else:
                    items.append({"type": item_type, "output": output})
                continue
            items.append({"type": item_type})
        final_text = self._extract_answer(result)
        if final_text:
            items.append({"type": "final_output", "final_output": final_text})
        return items
