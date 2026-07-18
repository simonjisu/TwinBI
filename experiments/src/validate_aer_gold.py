#!/usr/bin/env python3
"""Validate AER seed annotations against live dashboard inventory and chart queries."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = ROOT / "experiments/retrieval/gold_aer.json"
DEFAULT_OUTPUT = ROOT / "experiments/retrieval/generated/gold_validation.json"

TERM_ALIASES = {
    "store_name": "store_name",
    "product_type": "product_type",
    "department": "department",
    "category": "category",
    "quarter_label": "quarter_start",
    "avg_daily_sales": "avg_sales_per_day",
    "avg_revenue_per_unit": "revenue_per_unit",
    "qoq_growth_rate": "qoq_growth_rate",
    "previous_units": "previous_units",
    "units_sold": "total_units_sold",
    "scale": "total_units_sold",
}


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - local endpoint supplied by CLI
        return json.load(response)


def flattened_terms(value: object) -> str:
    return json.dumps(value, sort_keys=True).lower().replace("_", " ")


def normalized(value: str) -> str:
    return " ".join(value.lower().split())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    dashboard_id = gold["dashboard_id"]
    inventory = get_json(f"{args.api_base}/superset/dashboards/{dashboard_id}/charts")["charts"]
    charts = {int(chart["chart_id"]): chart for chart in inventory}
    validated = set(gold["validated_query_ids"])
    results: list[dict] = []

    for entry in gold["entries"]:
        chart_id = entry["primary_chart_id"]
        chart = charts.get(chart_id)
        checks: dict[str, bool] = {"chart_exists": chart is not None}
        if chart is not None:
            checks["chart_name_matches"] = normalized(chart["name"]) == normalized(
                gold["chart_inventory"][str(chart_id)]
            )
            checks["tab_matches"] = chart.get("tab", {}).get("name") == entry["tab"]
            query_payload = get_json(f"{args.api_base}/superset/charts/{chart_id}/queries")
            chart_terms = flattened_terms(query_payload.get("queries", []))
            supporting_ids = entry["supporting_chart_ids"]
            supporting_exist = all(supporting_id in charts for supporting_id in supporting_ids)
            checks["supporting_charts_exist"] = supporting_exist
            for supporting_id in supporting_ids:
                if supporting_id not in charts:
                    continue
                supporting_payload = get_json(
                    f"{args.api_base}/superset/charts/{supporting_id}/queries"
                )
                chart_terms += " " + flattened_terms(supporting_payload.get("queries", []))
            required_terms = [entry["measure"], *entry["dimensions"]]
            missing = [
                term
                for term in required_terms
                if TERM_ALIASES.get(term, term).replace("_", " ") not in chart_terms
            ]
            checks["schema_supports_measure_and_dimensions"] = not missing
        else:
            missing = [entry["measure"], *entry["dimensions"]]
            checks.update(
                {
                    "chart_name_matches": False,
                    "tab_matches": False,
                    "supporting_charts_exist": False,
                    "schema_supports_measure_and_dimensions": False,
                }
            )
        results.append(
            {
                "query_id": entry["query_id"],
                "primary_chart_id": chart_id,
                "included_in_evaluation": entry["query_id"] in validated,
                "checks": checks,
                "missing_schema_terms": missing,
            }
        )

    included = [result for result in results if result["included_in_evaluation"]]
    passed = [result for result in included if all(result["checks"].values())]
    report = {
        "dashboard_id": dashboard_id,
        "inventory_chart_count": len(charts),
        "seed_annotation_count": len(results),
        "validated_evaluation_count": len(included),
        "validated_evaluation_pass_count": len(passed),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if args.strict and len(passed) != len(included):
        sys.exit(1)


if __name__ == "__main__":
    main()
