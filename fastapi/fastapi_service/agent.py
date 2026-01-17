from __future__ import annotations
import duckdb
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from fastapi_service.superset import (
    fetch_chart_data_from_log,
    fetch_dashboard_charts,
)

try:
    from agents import Agent, ModelSettings, Runner, function_tool
except Exception as exc:  # pragma: no cover - optional dependency
    Agent = None
    ModelSettings = None
    Runner = None
    function_tool = None
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
            reasoning={"effort": "low"},
            verbosity="low",
            max_turns=30,
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
            ]
            if function_tool
            else [],
            model_settings=model_settings,
            instructions=(
                "You answer questions about the current dashboard and charts. "
                "Use the provided chart context when available. "
                "If no active chart is available, explore dashboards by listing charts "
                "and fetching relevant chart data as needed. "
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
            return items
        for item in new_items:
            item_type = getattr(item, "type", None) or "unknown_item"
            if item_type == "reasoning_item":
                items.append({"type": item_type, "detail": "OpenAI hides the content"})
                continue
            raw = getattr(item, "raw_item", None)
            if item_type == "tool_call_item":
                name = getattr(raw, "name", None)
                if name is None and isinstance(raw, dict):
                    name = raw.get("name")
                items.append({"type": item_type, "name": name or "unknown_tool"})
                continue
            if item_type == "tool_call_output_item":
                output = None
                if isinstance(raw, dict):
                    output = raw.get("output")
                items.append({"type": item_type, "output": output})
                continue
            items.append({"type": item_type})
        return items
