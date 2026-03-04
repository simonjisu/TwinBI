# Insight Seeker (`/insights` command)

Purpose: describe how the `/insights` shortcut bypasses normal orchestration to return a concise insight summary based on chat history and current chart context.

## Execution Flow (high level)
- **Entrypoint**: `POST /chat` or `/chat/stream` in `fastapi/fastapi_service/main.py` builds a `AgentContext` with Superset settings, DuckDB connection, and optional active chart metadata (latest log summary + sampled chart data).
- **Context packaging**: If an active chart exists, the request also includes a JSON `context` string (chart id/name, log summary, chart data, active Superset context). This `context` is passed to the agent as a system message.
- **Dispatch**: `AgentRunner.respond` / `respond_stream` (`fastapi/fastapi_service/agent.py`) checks `message.startswith("/insights")`. If true, the orchestrator is skipped and InsightSeeker runs directly.
- **Insight agent call**: `AgentRunner._call_insights` builds a small payload and invokes the InsightSeeker sub-agent (no tools) via the generic `_run_subagent` wrapper from the `agents` package.
- **Output**: The InsightSeeker prompt forces a JSON object with a single `answer` field. `_parse_json_answer` unwraps the string and that text is returned to the caller; raw JSON is preserved for debug.

## Data inputs available to InsightSeeker
- Recent chat `history` (last 50 turns) for narrative context.
- `trace_logs` from `AgentContext` (if upstream components populated it; optional).
- `active` chart context from `AgentContext.active_chart` (includes active dashboard/charts snapshot), plus any summarized chart log/data added in `main.py` when an active chart id is present.

## Pseudocode (non-streaming path)
```python
# fastapi/fastapi_service/main.py
async def chat():
    active_context = build_superset_active_context(...)
    chart_context_obj = AgentContext(settings, conn, chart_id?, chart_name?, chart_data?, active_chart=active_context)
    chart_context_json = json.dumps({...chart metadata...}) if chart_id else None
    answer, debug, raw = agent_runner.respond(
        message=payload.message,
        history=load_dialogue_history(session_id, 20),
        context=chart_context_json,
        context_obj=chart_context_obj,
        debug=True,
        model_name=payload.agent_model,
    )

# fastapi/fastapi_service/agent.py
async def respond(message, history, context, context_obj, ...):
    await _ensure_model(model_name)
    prompt = _build_input_messages(message, history, context=_inject_docs(context))
    _ACTIVE_CONTEXT = context_obj  # global for tool helpers
    if _is_insights_command(message) and _insight_seeker_agent:
        payload = {
            "mode": "insights",
            "history": history[-50:],
            "trace": getattr(context_obj, "trace_logs", None) if context_obj else None,
            "active": getattr(context_obj, "active_chart", None) if context_obj else None,
        }
        insights_json = _run_subagent(_insight_seeker_agent, json.dumps(payload, ensure_ascii=False))
        return _parse_json_answer(insights_json), [], insights_json
    result = _run_agent(_orchestrator_agent, prompt, context_obj)
    ...  # normal orchestration path
```

## InsightSeeker agent specifics
- Definition lives in `fastapi/fastapi_service/prompts.py` under `INSIGHT_SEEKER_AGENT`.
- Model: same as other agents (`AgentRunner._build_agents` picks `AGENT_MODEL` env or `gpt-5-nano`).
- Tools: none; it only consumes the provided payload.
- Required output: JSON `{"answer": "..."}` containing:
  - Observed user actions (charts viewed/drilled, filters, time ranges)
  - Current analytical context (slice/dimensions/measures)
  - 1–3 insights (include numbers if present; otherwise state insufficient data)
  - Recommended next deep-dive steps.
- Guardrails: must not fabricate data; should state when insufficient evidence exists.

## Streaming path differences
- `AgentRunner.respond_stream` mirrors the same `/insights` branch. It yields a single `final` event with the parsed answer and raw JSON; no orchestrator events are emitted because tools are not invoked.

## Error handling & fallbacks
- If the agents framework is unavailable (`agents` import fails), `_build_agents` returns `None` agents, causing `respond` to return a static “Agent not available” message.
- `_parse_json_answer` tolerates non-JSON output; if InsightSeeker returns plain text, it is passed through as-is.

