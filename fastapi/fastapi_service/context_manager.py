from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _metric_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    column = value.get("column")
    if isinstance(column, dict):
        column = column.get("column_name") or column.get("verbose_name")
    return (
        value.get("label")
        or value.get("sqlExpression")
        or value.get("metric_name")
        or column
    )


def _dimension_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    return (
        value.get("label")
        or value.get("column_name")
        or value.get("sqlExpression")
    )


def _unique_strings(values: list[Any], extractor) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = extractor(value)
        if name is None:
            continue
        normalized = str(name).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def _unique_objects(values: list[Any]) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for value in values:
        try:
            key = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        except TypeError:
            key = str(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def _has_concrete_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_has_concrete_value(child) for child in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_concrete_value(child) for child in value)
    return True


def _query_parts(
    form_data: dict[str, Any],
    queries: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[Any], dict[str, Any]]:
    query = next((item for item in queries if isinstance(item, dict)), {})
    metric_values = [
        *_list(form_data.get("metric")),
        *_list(form_data.get("metrics")),
        *_list(query.get("metric")),
        *_list(query.get("metrics")),
    ]
    dimension_values = [
        *_list(form_data.get("columns")),
        *_list(form_data.get("groupby")),
        *_list(form_data.get("series")),
        *_list(form_data.get("series_columns")),
        *_list(query.get("columns")),
        *_list(query.get("groupby")),
        *_list(query.get("series")),
        *_list(query.get("series_columns")),
    ]
    filters = _unique_objects(
        [
            *_list(form_data.get("filters")),
            *_list(query.get("filters")),
        ]
    )
    time_scope = {
        key: value
        for key, value in {
            "time_range": query.get("time_range") or form_data.get("time_range"),
            "time_grain": (
                query.get("time_grain_sqla")
                or form_data.get("time_grain_sqla")
                or form_data.get("granularity_sqla")
            ),
        }.items()
        if value not in (None, "")
    }
    return (
        _unique_strings(metric_values, _metric_name),
        _unique_strings(dimension_values, _dimension_name),
        filters,
        time_scope,
    )


def _tab_name(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("name") or value.get("id")
    return value


def _state_version(
    active_context: dict[str, Any],
    active_charts_context: list[dict[str, Any]],
) -> str:
    log_ids: list[int] = []
    for chart in active_charts_context:
        chart_log = chart.get("chart_log") if isinstance(chart, dict) else None
        log_id = chart_log.get("superset_log_id") if isinstance(chart_log, dict) else None
        try:
            log_ids.append(int(log_id))
        except (TypeError, ValueError):
            continue
    session = active_context.get("session_id") or "global"
    revision = max(log_ids) if log_ids else "current"
    return f"{session}:{revision}"


def validate_aer_record(record: dict[str, Any]) -> list[str]:
    """Return missing or invalid fields for one AER."""
    errors: list[str] = []
    for key in (
        "aer_id",
        "evidence_type",
        "source",
        "semantic_scope",
        "provenance",
        "freshness",
    ):
        if record.get(key) in (None, "", {}):
            errors.append(key)
    source = record.get("source")
    if not isinstance(source, dict):
        errors.append("source")
    elif not _has_concrete_value(source):
        errors.append("source.empty")
    semantic_scope = record.get("semantic_scope")
    if not isinstance(semantic_scope, dict):
        errors.append("semantic_scope")
    elif not any(
        _has_concrete_value(semantic_scope.get(key))
        for key in (
            "measures",
            "dimensions",
            "filters",
            "hierarchy",
            "time_scope",
        )
    ):
        errors.append("semantic_scope.empty")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance")
    elif not _has_concrete_value(provenance):
        errors.append("provenance.empty")
    freshness = record.get("freshness")
    if not isinstance(freshness, dict):
        errors.append("freshness")
    else:
        if not _has_concrete_value(freshness.get("state_version")):
            errors.append("freshness.state_version")
        if freshness.get("status") not in {"fresh", "stale"}:
            errors.append("freshness.status")
    if record.get("result_available") and record.get("result") is None:
        errors.append("result")
    return sorted(set(errors))


def build_aer_candidates(
    active_context: dict[str, Any] | None,
    active_charts_context: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build request-scoped AERs from the current projected state and chart logs."""
    if not isinstance(active_context, dict):
        return []
    active_tab = active_context.get("active_tab")
    state_version = _state_version(active_context, active_charts_context)
    candidates: list[dict[str, Any]] = []
    for chart in active_charts_context:
        if not isinstance(chart, dict):
            continue
        chart_log = chart.get("chart_log")
        if not isinstance(chart_log, dict):
            chart_log = {}
        form_data = chart_log.get("form_data")
        if not isinstance(form_data, dict):
            form_data = {}
        queries = [
            query
            for query in _list(chart_log.get("queries"))
            if isinstance(query, dict)
        ]
        measures, dimensions, query_filters, time_scope = _query_parts(
            form_data, queries
        )
        slice_id = chart.get("slice_id")
        log_id = chart_log.get("superset_log_id")
        native_filters = chart.get("native_filters") or []
        cross_filters = chart.get("cross_filters") or []
        result = chart_log.get("result")
        record = {
            "aer_id": f"existing-chart:{slice_id}:{log_id or 'current'}",
            "evidence_type": "existing-chart",
            "source": {
                "chart_id": slice_id,
                "chart_name": chart.get("name"),
                "datasource": chart_log.get("datasource"),
            },
            "semantic_scope": {
                "measures": measures,
                "dimensions": dimensions,
                "filters": _unique_objects(
                    [*query_filters, *_list(native_filters), *_list(cross_filters)]
                ),
                "hierarchy": {
                    "tab": _tab_name(active_tab),
                    "level": dimensions[-1] if dimensions else None,
                },
                "time_scope": time_scope,
            },
            "state_scope": {
                "dashboard_id": active_context.get("dashboard_id"),
                "session_id": active_context.get("session_id"),
                "active_tab": active_tab,
                "native_filters": native_filters,
                "cross_filters": cross_filters,
                "interaction": chart.get("interaction"),
                "state_version": state_version,
            },
            "query_context": {
                "form_data": form_data,
                "queries": queries,
                "datasource": chart_log.get("datasource"),
            },
            "result": result,
            "result_available": result is not None,
            "provenance": {
                "superset_log_id": log_id,
                "source_chart_id": slice_id,
                "action": chart_log.get("action"),
                "interaction": chart.get("interaction"),
            },
            "freshness": {
                "state_version": state_version,
                "observed_at": chart_log.get("observed_at"),
                "status": "fresh",
            },
        }
        record["validation_errors"] = validate_aer_record(record)
        candidates.append(record)
    return candidates


class AERStore:
    """Session-scoped latest AER snapshots used by the Context Manager."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._lock = RLock()

    @staticmethod
    def _key(active_context: dict[str, Any] | None) -> tuple[str, str]:
        context = active_context if isinstance(active_context, dict) else {}
        return (
            str(context.get("dashboard_id") or "global"),
            str(context.get("session_id") or "global"),
        )

    def refresh_snapshot(
        self,
        active_context: dict[str, Any] | None,
        active_charts_context: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Replace chart-backed AERs and retain valid on-demand AERs for this state."""
        context = active_context if isinstance(active_context, dict) else {}
        current = build_aer_candidates(context, active_charts_context)
        current_state_version = _state_version(context, active_charts_context)
        key = self._key(context)
        with self._lock:
            previous = deepcopy(self._records.get(key, []))
            existing_ids = {record.get("aer_id") for record in current}
            for record in previous:
                if record.get("evidence_type") != "on-demand-query":
                    continue
                freshness = record.get("freshness")
                if not isinstance(freshness, dict):
                    continue
                if freshness.get("state_version") != current_state_version:
                    continue
                if record.get("aer_id") in existing_ids:
                    continue
                freshness["status"] = "fresh"
                record["validation_errors"] = validate_aer_record(record)
                current.append(record)
                existing_ids.add(record.get("aer_id"))
            self._records[key] = current
            return current

    def snapshot(
        self,
        active_context: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._records.get(self._key(active_context), []))

    def clear(self, active_context: dict[str, Any] | None = None) -> None:
        with self._lock:
            if active_context is None:
                self._records.clear()
            else:
                self._records.pop(self._key(active_context), None)


def _decode_agent_payload(value: Any) -> Any:
    decoded = value
    for _ in range(3):
        if not isinstance(decoded, str):
            break
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            break
    if isinstance(decoded, dict) and isinstance(decoded.get("answer"), str):
        answer = _decode_agent_payload(decoded["answer"])
        if isinstance(answer, dict):
            return answer
    return decoded


def _find_values(value: Any, keys: set[str]) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys:
                found.extend(_list(child))
            found.extend(_find_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            found.extend(_find_values(child, keys))
    return found


def _chart_ids(*payloads: Any) -> set[str]:
    values = _find_values(
        list(payloads),
        {"chart_id", "source_chart_id", "slice_id"},
    )
    return {
        str(value)
        for value in values
        if value not in (None, "") and not isinstance(value, (dict, list))
    }


def _candidate_ids(payload: Any) -> set[str]:
    return {
        str(value)
        for value in _find_values(payload, {"candidate_aer_ids", "aer_ids"})
        if value not in (None, "") and not isinstance(value, (dict, list))
    }


def _tool_names(payload: Any) -> list[str]:
    return _unique_strings(
        _find_values(payload, {"tools_executed", "tool", "tool_name"}),
        lambda value: value if isinstance(value, str) else None,
    )


def _result_from_payload(payload: Any) -> Any | None:
    if not isinstance(payload, dict):
        return None
    for key in ("result", "raw_rows", "rows", "data", "values"):
        value = payload.get(key)
        if value not in (None, [], {}):
            return value
    return None


def _scope_from_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    query_details = payload.get("query_details")
    if not isinstance(query_details, dict):
        query_details = {}
    measures = _unique_strings(
        [
            *_list(query_details.get("metric")),
            *_list(query_details.get("metrics")),
            *_list(payload.get("metric")),
            *_list(payload.get("metrics")),
        ],
        _metric_name,
    )
    dimensions = _unique_strings(
        [
            *_list(query_details.get("group_by")),
            *_list(query_details.get("columns")),
            *_list(payload.get("group_by")),
            *_list(payload.get("columns")),
        ],
        _dimension_name,
    )
    filters = _unique_objects(
        [
            *_list(query_details.get("filters")),
            *_list(payload.get("filters")),
        ]
    )
    return {
        "measures": measures,
        "dimensions": dimensions,
        "filters": filters,
        "time_scope": {
            key: value
            for key, value in {
                "time_range": query_details.get("time_range")
                or payload.get("time_range"),
                "time_grain": query_details.get("time_grain")
                or payload.get("time_grain"),
            }.items()
            if value not in (None, "")
        },
    }


def _merge_scope(current: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key in ("measures", "dimensions", "filters"):
        if update.get(key):
            merged[key] = _unique_objects([*_list(current.get(key)), *update[key]])
    if update.get("time_scope"):
        merged["time_scope"] = {**current.get("time_scope", {}), **update["time_scope"]}
    hierarchy = dict(current.get("hierarchy") or {})
    dimensions = merged.get("dimensions") or []
    if dimensions:
        hierarchy["level"] = dimensions[-1]
    merged["hierarchy"] = hierarchy
    return merged


def _on_demand_record(
    payload: dict[str, Any],
    request_payload: Any,
    live_state: dict[str, Any] | None,
    state_version: str | None = None,
) -> dict[str, Any]:
    live_state = live_state if isinstance(live_state, dict) else {}
    result = _result_from_payload(payload)
    query_context = payload.get("query_details") or request_payload
    digest_source = {
        "query_context": query_context,
        "state": {
            "dashboard_id": live_state.get("dashboard_id"),
            "session_id": live_state.get("session_id"),
            "active_tab": live_state.get("active_tab"),
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_source, sort_keys=True, ensure_ascii=False, default=str
        ).encode("utf-8")
    ).hexdigest()[:16]
    effective_state_version = state_version or (
        f"{live_state.get('session_id') or 'global'}:on-demand:{digest}"
    )
    record = {
        "aer_id": f"on-demand-query:{digest}",
        "evidence_type": "on-demand-query",
        "source": {
            "chart_id": next(iter(_chart_ids(payload)), None),
            "dataset_id": next(
                iter(
                    str(value)
                    for value in _find_values(payload, {"dataset_id"})
                    if value not in (None, "")
                ),
                None,
            ),
        },
        "semantic_scope": {
            **_scope_from_payload(payload),
            "hierarchy": {"tab": _tab_name(live_state.get("active_tab"))},
        },
        "state_scope": {
            "dashboard_id": live_state.get("dashboard_id"),
            "session_id": live_state.get("session_id"),
            "active_tab": live_state.get("active_tab"),
            "state_version": effective_state_version,
        },
        "query_context": query_context,
        "result": result,
        "result_available": result is not None,
        "provenance": {
            "tools_executed": _tool_names(payload),
            "parent_aer_ids": sorted(_candidate_ids(request_payload)),
        },
        "freshness": {
            "state_version": effective_state_version,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": "fresh",
        },
    }
    record["validation_errors"] = validate_aer_record(record)
    return record


def refresh_aer_store(
    records: list[dict[str, Any]],
    chart_manager_output: Any,
    chart_manager_request: Any = None,
    live_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Bind Chart Manager results to existing AERs or create an on-demand AER."""
    payload = _decode_agent_payload(chart_manager_output)
    request_payload = _decode_agent_payload(chart_manager_request)
    if not isinstance(payload, dict):
        return []
    result = _result_from_payload(payload)
    if result is None:
        return []

    requested_aers = _candidate_ids(request_payload)
    output_chart_ids = _chart_ids(payload, request_payload)
    matched: list[dict[str, Any]] = []
    for record in records:
        source = record.get("source")
        chart_id = source.get("chart_id") if isinstance(source, dict) else None
        if (
            record.get("aer_id") in requested_aers
            or (chart_id is not None and str(chart_id) in output_chart_ids)
        ):
            matched.append(record)

    query_tools = set(_tool_names(payload))
    is_on_demand = bool(
        query_tools
        & {
            "query_superset_dataset",
            "query_cube",
            "fetch_dataset_data",
        }
    )
    if is_on_demand or not matched:
        parent_state_version = next(
            (
                item.get("freshness", {}).get("state_version")
                for item in records
                if item.get("aer_id") in requested_aers
                and isinstance(item.get("freshness"), dict)
                and item.get("freshness", {}).get("state_version")
            ),
            None,
        )
        record = _on_demand_record(
            payload,
            request_payload,
            live_state,
            state_version=parent_state_version,
        )
        existing = next(
            (item for item in records if item.get("aer_id") == record["aer_id"]),
            None,
        )
        if existing is None:
            records.append(record)
            return [record]
        existing.clear()
        existing.update(record)
        return [existing]

    now = datetime.now(timezone.utc).isoformat()
    scope_update = _scope_from_payload(payload)
    for record in matched:
        record["result"] = result
        record["result_available"] = True
        record["semantic_scope"] = _merge_scope(
            record.get("semantic_scope") or {}, scope_update
        )
        provenance = dict(record.get("provenance") or {})
        provenance["tools_executed"] = _tool_names(payload)
        provenance["chart_manager_request_aer_ids"] = sorted(requested_aers)
        record["provenance"] = provenance
        freshness = dict(record.get("freshness") or {})
        freshness.update({"observed_at": now, "status": "fresh"})
        record["freshness"] = freshness
        record["validation_errors"] = validate_aer_record(record)
    return matched


def attach_aer_records(chart_manager_output: Any, records: list[dict[str, Any]]) -> str:
    """Return a Chart Manager tool result that includes the refreshed AER bundle."""
    payload = _decode_agent_payload(chart_manager_output)
    if not isinstance(payload, dict):
        payload = {"chart_manager_output": payload}
    payload["aer_records"] = records
    return json.dumps(
        {"answer": json.dumps(payload, ensure_ascii=False, default=str)},
        ensure_ascii=False,
    )


def build_answer_evidence_payload(payload_json: Any, records: list[dict[str, Any]]) -> str:
    """Attach validated AER evidence to an Answer Composer request."""
    payload = _decode_agent_payload(payload_json)
    if not isinstance(payload, dict):
        payload = {"requested_answer": payload}
    evidence: list[dict[str, Any]] = []
    for record in records:
        validation_errors = validate_aer_record(record)
        record["validation_errors"] = validation_errors
        freshness = record.get("freshness")
        if (
            record.get("result_available")
            and not validation_errors
            and isinstance(freshness, dict)
            and freshness.get("status") == "fresh"
        ):
            evidence.append(record)
    payload["aer_evidence"] = evidence
    return json.dumps(payload, ensure_ascii=False, default=str)
