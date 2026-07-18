#!/usr/bin/env python3
"""Materialize verified query-result AERs for tasks unsupported by visible charts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = ROOT / "experiments/retrieval/gold_aer.json"
DEFAULT_OUTPUT = ROOT / "experiments/retrieval/generated/query_result_aers.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    selected = set(gold["query_result_aer_query_ids"])
    entries = {entry["query_id"]: entry for entry in gold["entries"]}
    records: list[dict] = []
    for query_id in sorted(selected):
        entry = entries[query_id]
        answer_payload = json.loads(
            (ROOT / "experiments/queries" / entry["answer_file"]).read_text(encoding="utf-8")
        )
        final_answer = answer_payload["final_answer"]
        methods = answer_payload["methods"]
        method_answers = {
            name: value.get("answer")
            for name, value in methods.items()
            if isinstance(value, dict) and "answer" in value
        }
        if set(method_answers) != {"db_sql", "cube_api", "our_api_plus_superset"}:
            raise ValueError(f"{query_id}: missing independent verification method")
        if any(answer != final_answer for answer in method_answers.values()):
            raise ValueError(f"{query_id}: method answers do not match final_answer")
        contexts = {
            name: value.get("context", {})
            for name, value in methods.items()
            if isinstance(value, dict)
        }
        records.append(
            {
                "aer_id": f"query-result:{query_id.lower()}",
                "record_type": "query_result",
                "query_id": query_id,
                "measure": entry["measure"],
                "dimensions": entry["dimensions"],
                "filters": entry["filters"],
                "hierarchy": entry["tab"],
                "result": final_answer,
                "provenance": {
                    "verification_methods": sorted(method_answers),
                    "method_contexts": contexts,
                },
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    print(f"Wrote {len(records)} verified query-result AERs to {args.output}")


if __name__ == "__main__":
    main()
