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


def state_for(entry: dict) -> dict:
    supporting = entry["supporting_chart_ids"]
    focused_chart_id = entry["primary_chart_id"]
    if entry["interaction"] == "cross_filter" and supporting:
        focused_chart_id = supporting[0]
    return {
        "dashboard_id": 13,
        "active_tab": entry["tab"],
        "filters": entry["filters"],
        # A dashboard interaction usually focuses a control/chart, not necessarily
        # the chart that ultimately answers the question.
        "focused_chart_id": focused_chart_id,
        "interaction": entry["interaction"],
    }


def query_variants(entry: dict, explicit: str) -> list[tuple[str, str, str | None]]:
    target = entry["target"].replace("_", " ")
    tab = entry["tab"]
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
    args = parser.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for entry in gold["entries"]:
        explicit = task_text(ROOT / "experiments/queries" / entry["task_file"])
        for variant, query, history in query_variants(entry, explicit):
            rows.append(
                {
                    "session_id": f"{entry['query_id'].lower()}-{variant}",
                    "query_id": entry["query_id"],
                    "variant": variant,
                    "query": query,
                    "dialogue_history": history,
                    "state": state_for(entry),
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
    print(f"Wrote {len(rows)} query-session pairs to {args.output}")


if __name__ == "__main__":
    main()
