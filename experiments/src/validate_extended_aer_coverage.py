#!/usr/bin/env python3
"""Validate that chart and query-result AERs cover every benchmark task once."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = ROOT / "experiments/retrieval/gold_aer.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--query-result-aers", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    chart_ids = set(gold["validated_query_ids"])
    query_records = [
        json.loads(line)
        for line in args.query_result_aers.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    query_ids = {record["query_id"] for record in query_records}
    all_ids = {entry["query_id"] for entry in gold["entries"]}
    overlap = sorted(chart_ids & query_ids)
    missing = sorted(all_ids - chart_ids - query_ids)
    unexpected = sorted((chart_ids | query_ids) - all_ids)
    report = {
        "task_count": len(all_ids),
        "visible_chart_aer_count": len(chart_ids),
        "query_result_aer_count": len(query_ids),
        "overlap": overlap,
        "missing": missing,
        "unexpected": unexpected,
        "coverage_complete": not overlap and not missing and not unexpected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not report["coverage_complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
