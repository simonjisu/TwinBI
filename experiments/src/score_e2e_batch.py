#!/usr/bin/env python3
"""Score an end-to-end batch against the task-level gold answer files."""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUERIES = ROOT / "experiments/queries"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--queries-dir", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args()


def _parse_answer(value: str) -> Any:
    text = value.strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json|python)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        for loader in (json.loads, ast.literal_eval):
            try:
                return loader(candidate)
            except (ValueError, SyntaxError, json.JSONDecodeError):
                continue
    return text


def _leaf_matches(gold: Any, predicted: Any) -> tuple[int, int]:
    if isinstance(gold, dict):
        matched = total = 0
        for key, value in gold.items():
            child_matched, child_total = _leaf_matches(value, predicted.get(key) if isinstance(predicted, dict) else None)
            matched += child_matched
            total += child_total
        return matched, total
    if isinstance(gold, list):
        matched = total = 0
        for index, value in enumerate(gold):
            item = predicted[index] if isinstance(predicted, list) and index < len(predicted) else None
            child_matched, child_total = _leaf_matches(value, item)
            matched += child_matched
            total += child_total
        return matched, total
    if isinstance(gold, (int, float)) and not isinstance(gold, bool):
        try:
            return int(math.isclose(float(gold), float(predicted), rel_tol=1e-4, abs_tol=1e-6)), 1
        except (TypeError, ValueError):
            return 0, 1
    return int(gold == predicted), 1


def _load_metadata(batch_dir: Path, row: dict[str, str]) -> dict[str, Any]:
    trace_dir = row.get("trace_dir", "").strip()
    candidates = [batch_dir / row["query"] / "run_meta.json"]
    if trace_dir:
        candidates.append(ROOT / trace_dir / "run_meta.json")
    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def main() -> int:
    args = _parse_args()
    batch_dir = args.batch_dir.resolve()
    summary_path = batch_dir / "summary.csv"
    if not summary_path.exists():
        raise SystemExit(f"Missing summary.csv: {summary_path}")
    output_path = args.output or batch_dir / "scored_report.json"

    with summary_path.open(encoding="utf-8", newline="") as handle:
        summary_rows = list(csv.DictReader(handle))

    results: list[dict[str, Any]] = []
    for row in summary_rows:
        query = row["query"]
        gold_path = args.queries_dir / f"{query}_ans.json"
        gold = json.loads(gold_path.read_text(encoding="utf-8"))["final_answer"]
        metadata = _load_metadata(batch_dir, row)
        raw_answer = str(metadata.get("final_answer", row.get("final_answer", "")))
        predicted = _parse_answer(raw_answer)
        matched, total = _leaf_matches(gold, predicted)
        required_state = bool(metadata.get("scenario_requires_dashboard_action", False))
        state_verified = bool(metadata.get("dashboard_action_verified", False))
        invalid_state = required_state and not state_verified
        model_evidence = metadata.get("streamlit_model_evidence")
        model_verified = not isinstance(model_evidence, dict) or bool(model_evidence.get("verified"))
        apply_meta_present = "apply_meta_clicked" in metadata
        apply_meta_clicked = bool(metadata.get("apply_meta_clicked")) if apply_meta_present else True
        invalid_setup = not model_verified or not apply_meta_clicked
        if invalid_state or invalid_setup:
            matched = 0
        exact = total > 0 and matched == total and not invalid_state and not invalid_setup
        timed_out = (
            row.get("exit_code") == "124"
            or "timeout" in raw_answer.lower()
            or "timed out" in raw_answer.lower()
        )
        status = "exact" if exact else "partial" if matched else "failed"
        results.append(
            {
                "query": query,
                "status": status,
                "matched_leaves": matched,
                "gold_leaves": total,
                "timed_out": timed_out,
                "required_state": required_state,
                "state_verified": state_verified,
                "invalid_state": invalid_state,
                "model_verified": model_verified,
                "apply_meta_clicked": apply_meta_clicked,
                "invalid_setup": invalid_setup,
                "scenario_steps": int(metadata.get("scenario_steps_executed") or 0),
                "login_success": bool(metadata.get("login_success")),
                "gold": gold,
                "prediction": predicted,
                "raw_answer": raw_answer,
            }
        )

    count = len(results)
    exact_count = sum(item["status"] == "exact" for item in results)
    partial_count = sum(item["status"] == "partial" for item in results)
    report = {
        "batch_dir": str(batch_dir),
        "task_count": count,
        "exact_count": exact_count,
        "partial_count": partial_count,
        "failed_count": count - exact_count - partial_count,
        "exact_rate": round(exact_count / count, 4) if count else 0.0,
        "partial_or_exact_rate": round((exact_count + partial_count) / count, 4) if count else 0.0,
        "mean_scenario_steps": round(sum(item["scenario_steps"] for item in results) / count, 2) if count else 0.0,
        "timeout_count": sum(item["timed_out"] for item in results),
        "invalid_state_count": sum(item["invalid_state"] for item in results),
        "invalid_setup_count": sum(item["invalid_setup"] for item in results),
        "login_success_count": sum(item["login_success"] for item in results),
        "results": results,
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
