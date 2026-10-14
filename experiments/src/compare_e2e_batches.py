#!/usr/bin/env python3
"""Build an auditable paired comparison from two scored E2E batches."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ACTION_FIELDS = (
    "dashboard_actions_attempted",
    "dashboard_actions_dispatched",
    "dashboard_actions_visible_target",
    "dashboard_actions_visual_change",
    "dashboard_actions_no_visual_change",
    "dashboard_actions_blocked",
    "dashboard_actions_dom_dispatch",
    "dashboard_actions_coordinate_dispatch",
    "dashboard_actions_occlusion_override",
)
STATUS_RANK = {"failed": 0, "partial": 1, "exact": 2}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-aware-dir", type=Path, required=True)
    parser.add_argument("--dashboard-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_meta(batch_dir: Path, query: str) -> dict[str, Any]:
    path = batch_dir / query / "run_meta.json"
    return _load_json(path) if path.exists() else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_audit(batch_dir: Path) -> dict[str, Any]:
    action_types = {"click", "hover", "type", "scroll"}
    failures: list[dict[str, str]] = []
    trace_count = action_count = dispatched_count = 0
    point_target_count = screenshot_pair_count = screenshot_hash_count = 0
    truthful_change_flag_count = 0
    step_session_ids: set[str] = set()

    for steps_path in sorted(batch_dir.glob("query_*/steps.jsonl")):
        trace_count += 1
        for line_number, line in enumerate(steps_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            page_url = str(record.get("url") or "")
            if page_url:
                session_id = parse_qs(urlparse(page_url).query).get("session_id", [""])[0]
                if session_id:
                    step_session_ids.add(session_id)
            executed = record.get("executed") or {}
            action_type = executed.get("type")
            if action_type not in action_types:
                continue
            action_count += 1
            if executed.get("action_dispatched"):
                dispatched_count += 1
            target = executed.get("target") or {}
            point_target = target.get("point_target_before") or executed.get("point_target_before") or {}
            if point_target.get("found"):
                point_target_count += 1
            else:
                failures.append(
                    {"trace": str(steps_path), "line": str(line_number), "reason": "missing visible point target"}
                )

            before_name = executed.get("before_screenshot")
            after_name = executed.get("after_screenshot")
            if not before_name or not after_name:
                failures.append(
                    {"trace": str(steps_path), "line": str(line_number), "reason": "missing screenshot name"}
                )
                continue
            before_path = steps_path.parent / before_name
            after_path = steps_path.parent / after_name
            if not before_path.exists() or not after_path.exists():
                failures.append(
                    {"trace": str(steps_path), "line": str(line_number), "reason": "missing screenshot file"}
                )
                continue
            screenshot_pair_count += 1
            before_hash = _sha256(before_path)
            after_hash = _sha256(after_path)
            if before_hash == executed.get("before_sha256") and after_hash == executed.get("after_sha256"):
                screenshot_hash_count += 2
            else:
                failures.append(
                    {"trace": str(steps_path), "line": str(line_number), "reason": "screenshot hash mismatch"}
                )
            if bool(executed.get("screenshot_changed")) == (before_hash != after_hash):
                truthful_change_flag_count += 1
            else:
                failures.append(
                    {"trace": str(steps_path), "line": str(line_number), "reason": "incorrect screenshot_changed flag"}
                )

    return {
        "trace_file_count": trace_count,
        "evidence_action_count": action_count,
        "action_dispatched_count": dispatched_count,
        "visible_point_target_count": point_target_count,
        "screenshot_pair_count": screenshot_pair_count,
        "screenshot_hash_verified_count": screenshot_hash_count,
        "truthful_screenshot_change_flag_count": truthful_change_flag_count,
        "unique_streamlit_session_count_from_steps": len(step_session_ids),
        "failure_count": len(failures),
        "failures": failures,
        "all_evidence_valid": not failures,
    }


def _action_summary(batch_dir: Path, results: list[dict[str, Any]]) -> dict[str, Any]:
    totals = Counter({field: 0 for field in ACTION_FIELDS})
    budgets: list[int] = []
    sessions: list[str] = []
    meta_count = 0
    exhausted: list[str] = []
    blocked: list[str] = []
    occluded: list[str] = []
    model_verified = 0
    apply_meta_clicked = 0
    state_verified = 0

    for result in results:
        query = result["query"]
        meta = _load_meta(batch_dir, query)
        if not meta:
            continue
        meta_count += 1
        for field in ACTION_FIELDS:
            totals[field] += int(meta.get(field) or 0)
        budget = int(meta.get("dashboard_action_budget") or 0)
        attempted = int(meta.get("dashboard_actions_attempted") or 0)
        if budget:
            budgets.append(budget)
        if budget and attempted >= budget:
            exhausted.append(query)
        if int(meta.get("dashboard_actions_blocked") or 0):
            blocked.append(query)
        if int(meta.get("dashboard_actions_occlusion_override") or 0):
            occluded.append(query)
        session_id = str(meta.get("session_id") or "")
        if session_id:
            sessions.append(session_id)
        model_evidence = meta.get("streamlit_model_evidence")
        if not isinstance(model_evidence, dict) or model_evidence.get("verified"):
            model_verified += 1
        if "apply_meta_clicked" not in meta or meta.get("apply_meta_clicked"):
            apply_meta_clicked += 1
        if result.get("state_verified"):
            state_verified += 1

    dispatched = totals["dashboard_actions_dispatched"]
    summary: dict[str, Any] = dict(totals)
    summary.update(
        {
            "run_meta_count": meta_count,
            "configured_budgets": sorted(set(budgets)),
            "budget_exhausted_count": len(exhausted),
            "budget_exhausted_queries": exhausted,
            "blocked_action_query_count": len(blocked),
            "blocked_action_queries": blocked,
            "occlusion_override_query_count": len(occluded),
            "occlusion_override_queries": occluded,
            "visible_target_rate_per_dispatch": round(
                totals["dashboard_actions_visible_target"] / dispatched, 4
            )
            if dispatched
            else 0.0,
            "visual_change_rate_per_dispatch": round(
                totals["dashboard_actions_visual_change"] / dispatched, 4
            )
            if dispatched
            else 0.0,
            "unique_streamlit_session_count": len(set(sessions)),
            "model_verified_count": model_verified,
            "apply_meta_clicked_count": apply_meta_clicked,
            "state_verified_count": state_verified,
        }
    )
    return summary


def _condition_summary(report: dict[str, Any], batch_dir: Path) -> dict[str, Any]:
    keys = (
        "task_count",
        "exact_count",
        "partial_count",
        "failed_count",
        "exact_rate",
        "partial_or_exact_rate",
        "mean_scenario_steps",
        "timeout_count",
        "invalid_state_count",
        "invalid_setup_count",
        "login_success_count",
    )
    summary = {key: report[key] for key in keys}
    summary["action_evidence"] = _action_summary(batch_dir, report["results"])
    summary["trace_evidence_audit"] = _evidence_audit(batch_dir)
    return summary


def _question_record(
    query: str,
    state: dict[str, Any],
    dashboard: dict[str, Any],
    state_dir: Path,
    dashboard_dir: Path,
) -> dict[str, Any]:
    state_meta = _load_meta(state_dir, query)
    dashboard_meta = _load_meta(dashboard_dir, query)

    def condition(result: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        record = {
            "status": result["status"],
            "matched_leaves": result["matched_leaves"],
            "gold_leaves": result["gold_leaves"],
            "prediction": result["prediction"],
            "raw_answer": result["raw_answer"],
            "scenario_steps": result["scenario_steps"],
            "login_success": result["login_success"],
            "state_verified": result["state_verified"],
            "run_meta_present": bool(meta),
            "session_id": meta.get("session_id"),
            "trace_dir": meta.get("trace_dir"),
            "steps_jsonl": meta.get("steps_jsonl"),
            "dashboard_action_budget": int(meta.get("dashboard_action_budget") or 0),
        }
        for field in ACTION_FIELDS:
            record[field] = int(meta.get(field) or 0)
        return record

    state_rank = STATUS_RANK[state["status"]]
    dashboard_rank = STATUS_RANK[dashboard["status"]]
    winner = "state_aware" if state_rank > dashboard_rank else "dashboard_only" if dashboard_rank > state_rank else "tie"
    return {
        "query": query,
        "winner": winner,
        "gold": state["gold"],
        "state_aware": condition(state, state_meta),
        "dashboard_only": condition(dashboard, dashboard_meta),
    }


def _write_csv(path: Path, questions: list[dict[str, Any]]) -> None:
    fields = [
        "query",
        "winner",
        "state_status",
        "dashboard_status",
        "state_matched_leaves",
        "dashboard_matched_leaves",
        "gold_leaves",
        "state_prediction",
        "dashboard_prediction",
        "state_actions_attempted",
        "dashboard_actions_attempted",
        "state_actions_dispatched",
        "dashboard_actions_dispatched",
        "state_actions_visible_target",
        "dashboard_actions_visible_target",
        "state_actions_blocked",
        "dashboard_actions_blocked",
        "state_session_id",
        "state_trace_dir",
        "dashboard_trace_dir",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in questions:
            state = item["state_aware"]
            dashboard = item["dashboard_only"]
            writer.writerow(
                {
                    "query": item["query"],
                    "winner": item["winner"],
                    "state_status": state["status"],
                    "dashboard_status": dashboard["status"],
                    "state_matched_leaves": state["matched_leaves"],
                    "dashboard_matched_leaves": dashboard["matched_leaves"],
                    "gold_leaves": state["gold_leaves"],
                    "state_prediction": json.dumps(state["prediction"], ensure_ascii=False),
                    "dashboard_prediction": json.dumps(dashboard["prediction"], ensure_ascii=False),
                    "state_actions_attempted": state["dashboard_actions_attempted"],
                    "dashboard_actions_attempted": dashboard["dashboard_actions_attempted"],
                    "state_actions_dispatched": state["dashboard_actions_dispatched"],
                    "dashboard_actions_dispatched": dashboard["dashboard_actions_dispatched"],
                    "state_actions_visible_target": state["dashboard_actions_visible_target"],
                    "dashboard_actions_visible_target": dashboard["dashboard_actions_visible_target"],
                    "state_actions_blocked": state["dashboard_actions_blocked"],
                    "dashboard_actions_blocked": dashboard["dashboard_actions_blocked"],
                    "state_session_id": state["session_id"] or "",
                    "state_trace_dir": state["trace_dir"] or "",
                    "dashboard_trace_dir": dashboard["trace_dir"] or "",
                }
            )


def _write_markdown(path: Path, comparison: dict[str, Any]) -> None:
    state = comparison["summary"]["state_aware"]
    dashboard = comparison["summary"]["dashboard_only"]
    paired = comparison["paired_outcomes"]
    lines = [
        "# TwinBI paired E2E comparison",
        "",
        "| Condition | Exact | Partial | Failed | Exact rate | Partial+exact | Mean steps | Login |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        f"| State-aware | {state['exact_count']} | {state['partial_count']} | {state['failed_count']} | {state['exact_rate']:.4f} | {state['partial_or_exact_rate']:.4f} | {state['mean_scenario_steps']:.2f} | {state['login_success_count']}/30 |",
        f"| Dashboard-only | {dashboard['exact_count']} | {dashboard['partial_count']} | {dashboard['failed_count']} | {dashboard['exact_rate']:.4f} | {dashboard['partial_or_exact_rate']:.4f} | {dashboard['mean_scenario_steps']:.2f} | {dashboard['login_success_count']}/30 |",
        "",
        f"Paired outcomes: state-aware wins {paired['state_aware_wins']}, dashboard-only wins {paired['dashboard_only_wins']}, ties {paired['ties']}.",
        "",
        "## Interaction evidence",
        "",
        "| Condition | Attempted | Dispatched | Visible targets | Visual changes | Blocked | Occlusion overrides | Budget exhausted |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, item in (("State-aware", state), ("Dashboard-only", dashboard)):
        action = item["action_evidence"]
        lines.append(
            f"| {label} | {action['dashboard_actions_attempted']} | {action['dashboard_actions_dispatched']} | {action['dashboard_actions_visible_target']} | {action['dashboard_actions_visual_change']} | {action['dashboard_actions_blocked']} | {action['dashboard_actions_occlusion_override']} | {action['budget_exhausted_count']} |"
        )
    lines.extend(
        [
            "",
            "All click/hover/type/scroll evidence was independently re-hashed from the saved screenshots:",
            "",
            "| Condition | Trace files | Evidence actions | Screenshot pairs | Hashes verified | Evidence failures | Fresh Streamlit sessions from traces |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for label, item in (("State-aware", state), ("Dashboard-only", dashboard)):
        audit = item["trace_evidence_audit"]
        lines.append(
            f"| {label} | {audit['trace_file_count']} | {audit['evidence_action_count']} | {audit['screenshot_pair_count']} | {audit['screenshot_hash_verified_count']} | {audit['failure_count']} | {audit['unique_streamlit_session_count_from_steps']} |"
        )
    lines.extend(
        [
            "",
            "## Per-question outcome",
            "",
            "| Query | State-aware | Dashboard-only | Winner | State actions | Dashboard actions |",
            "|---|---|---|---|---:|---:|",
        ]
    )
    for item in comparison["questions"]:
        state_item = item["state_aware"]
        dashboard_item = item["dashboard_only"]
        lines.append(
            f"| {item['query']} | {state_item['status']} ({state_item['matched_leaves']}/{state_item['gold_leaves']}) | {dashboard_item['status']} ({dashboard_item['matched_leaves']}/{dashboard_item['gold_leaves']}) | {item['winner']} | {state_item['dashboard_actions_attempted']} | {dashboard_item['dashboard_actions_attempted']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = _parse_args()
    state_dir = args.state_aware_dir.resolve()
    dashboard_dir = args.dashboard_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    state_report = _load_json(state_dir / "scored_report.json")
    dashboard_report = _load_json(dashboard_dir / "scored_report.json")
    state_by_query = {item["query"]: item for item in state_report["results"]}
    dashboard_by_query = {item["query"]: item for item in dashboard_report["results"]}
    if state_by_query.keys() != dashboard_by_query.keys():
        raise SystemExit("The two batches do not contain the same query set")

    questions = [
        _question_record(
            query,
            state_by_query[query],
            dashboard_by_query[query],
            state_dir,
            dashboard_dir,
        )
        for query in sorted(state_by_query)
    ]
    winner_counts = Counter(item["winner"] for item in questions)
    comparison = {
        "state_aware_dir": str(state_dir),
        "dashboard_only_dir": str(dashboard_dir),
        "summary": {
            "state_aware": _condition_summary(state_report, state_dir),
            "dashboard_only": _condition_summary(dashboard_report, dashboard_dir),
            "delta_state_minus_dashboard": {
                "exact_count": state_report["exact_count"] - dashboard_report["exact_count"],
                "partial_count": state_report["partial_count"] - dashboard_report["partial_count"],
                "failed_count": state_report["failed_count"] - dashboard_report["failed_count"],
                "exact_rate": round(state_report["exact_rate"] - dashboard_report["exact_rate"], 4),
                "partial_or_exact_rate": round(
                    state_report["partial_or_exact_rate"] - dashboard_report["partial_or_exact_rate"], 4
                ),
                "mean_scenario_steps": round(
                    state_report["mean_scenario_steps"] - dashboard_report["mean_scenario_steps"], 2
                ),
            },
        },
        "paired_outcomes": {
            "state_aware_wins": winner_counts["state_aware"],
            "dashboard_only_wins": winner_counts["dashboard_only"],
            "ties": winner_counts["tie"],
        },
        "questions": questions,
    }

    json_path = output_dir / "paired_comparison.json"
    csv_path = output_dir / "paired_comparison.csv"
    markdown_path = output_dir / "paired_comparison.md"
    json_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_csv(csv_path, questions)
    _write_markdown(markdown_path, comparison)
    print(json.dumps({"json": str(json_path), "csv": str(csv_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
