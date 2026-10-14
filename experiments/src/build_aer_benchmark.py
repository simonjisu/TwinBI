#!/usr/bin/env python3
"""Build a deterministic offline AER retrieval benchmark from curated gold labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = ROOT / "experiments/retrieval/gold_aer.json"
DEFAULT_OUTPUT = ROOT / "experiments/retrieval/generated/query_sessions.jsonl"


def task_text(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    useful: list[str] = []
    for line in lines[1:]:
        if line.startswith("Output Format:") or line.startswith("Dashboard policy:"):
            break
        if line.strip():
            useful.append(line.strip())
    return " ".join(useful)


def state_for(entry: dict, chart_ids: list[int], tabs: list[str], condition: str) -> dict:
    supporting = entry["supporting_chart_ids"]
    focused_chart_id = None
    if entry["interaction"] == "cross_filter" and supporting:
        focused_chart_id = supporting[0]
    elif entry["interaction"] == "hover":
        focused_chart_id = entry["primary_chart_id"]
    state = {
        "dashboard_id": 13,
        "active_tab": entry["tab"],
        "filters": entry["filters"],
        # A dashboard interaction usually focuses a control/chart, not necessarily
        # the chart that ultimately answers the question.
        "focused_chart_id": focused_chart_id,
        "interaction": entry["interaction"],
    }
    excluded = set(entry["acceptable_chart_ids"]) | set(entry["supporting_chart_ids"])
    distractors = [chart_id for chart_id in chart_ids if chart_id not in excluded]
    if condition in {"stale_focus", "conflicting_state"} and distractors:
        state["focused_chart_id"] = distractors[sum(ord(char) for char in entry["query_id"]) % len(distractors)]
    if condition == "cleared_filters":
        state["filters"] = {}
    if condition in {"wrong_tab", "conflicting_state"}:
        alternatives = [tab for tab in tabs if tab != entry["tab"]]
        state["active_tab"] = alternatives[sum(ord(char) for char in entry["query_id"]) % len(alternatives)]
    return state


def query_variants(entry: dict, explicit: str) -> list[tuple[str, str, str | None]]:
    return [
        ("explicit", explicit, None),
        (
            "context_dependent",
            "Which result answers the current dashboard request?",
            "The user is reviewing the current dashboard view.",
        ),
        (
            "elliptical_followup",
            "And this one?",
            "Continue from the dashboard element currently in focus.",
        ),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--include-unvalidated-seeds",
        action="store_true",
        help="Include seed annotations not validated against the live chart schema.",
    )
    parser.add_argument(
        "--state-condition",
        choices=("oracle", "stale_focus", "cleared_filters", "wrong_tab", "conflicting_state", "all"),
        default="oracle",
        help="Generate oracle state or deterministic noisy-state stress conditions.",
    )
    args = parser.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    rows: list[dict] = []
    validated = set(gold.get("validated_query_ids", []))
    entries = gold["entries"]
    if not args.include_unvalidated_seeds:
        entries = [entry for entry in entries if entry["query_id"] in validated]
    chart_ids = sorted(int(chart_id) for chart_id in gold["chart_inventory"])
    tabs = sorted({entry["tab"] for entry in gold["entries"]})
    conditions = (
        ("oracle", "stale_focus", "cleared_filters", "wrong_tab", "conflicting_state")
        if args.state_condition == "all"
        else (args.state_condition,)
    )
    for entry in entries:
        explicit = task_text(ROOT / "experiments/queries" / entry["task_file"])
        for condition in conditions:
            for variant, query, history in query_variants(entry, explicit):
                suffix = "" if condition == "oracle" else f"-{condition}"
                rows.append(
                    {
                        "session_id": f"{entry['query_id'].lower()}-{variant}{suffix}",
                        "query_id": entry["query_id"],
                        "variant": variant,
                        "state_condition": condition,
                        "query": query,
                        "dialogue_history": history,
                        "state": state_for(entry, chart_ids, tabs, condition),
                        "gold": {
                            "primary_chart_id": entry["primary_chart_id"],
                            "acceptable_chart_ids": entry["acceptable_chart_ids"],
                            "measure": entry["measure"],
                            "dimensions": entry["dimensions"],
                            "filters": entry["filters"],
                            "hierarchy": entry["tab"],
                            "target": entry["target"],
                        },
                    }
                )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(
        f"Wrote {len(rows)} query-session pairs "
        f"({len(entries)} tasks; {', '.join(conditions)} state; {'all seeds' if args.include_unvalidated_seeds else 'validated subset'}) "
        f"to {args.output}"
    )


if __name__ == "__main__":
    main()
