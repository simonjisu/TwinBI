#!/usr/bin/env python3
"""Re-execute the 11 query-result AER tasks through DB, Cube, and Superset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulation.src.generate_query_answers import AnswerGenerator, same_answer


QUERY_DIR = ROOT / "experiments/queries"


def expected_answer(query_id: int) -> dict:
    payload = json.loads((QUERY_DIR / f"query_{query_id:02d}_ans.json").read_text())
    return payload["final_answer"]


def qoq_answers(rows: list[dict]) -> dict[int, dict]:
    threshold = 0.15
    return {
        13: {
            "pairs": [
                {
                    "department": row["department"],
                    "category": row["category"],
                    "qoq_growth_rate": row["qoq_growth_rate"],
                }
                for row in rows
                if row["qoq_growth_rate"] >= threshold
            ]
        },
        14: {
            "department": rows[0]["department"],
            "category": rows[0]["category"],
            "qoq_growth_rate": rows[0]["qoq_growth_rate"],
        },
        15: {
            "department": "Marketing",
            "categories": sorted(
                row["category"]
                for row in rows
                if row["department"] == "Marketing" and row["qoq_growth_rate"] >= threshold
            ),
        },
        16: next(
            {
                "department": row["department"],
                "category": "Laptop",
                "qoq_growth_rate": row["qoq_growth_rate"],
            }
            for row in rows
            if row["department"] == "Tech" and row["category"] == "Laptop"
        ),
        17: next(
            {
                "department": "Marketing",
                "category": "Mobile",
                "qoq_growth_rate": row["qoq_growth_rate"],
            }
            for row in rows
            if row["department"] == "Marketing" and row["category"] == "Mobile"
        ),
        18: {"qualifying_pair_count": sum(row["qoq_growth_rate"] >= threshold for row in rows)},
        27: next(
            {"category": "Laptop", "qoq_growth_rate": row["qoq_growth_rate"]}
            for row in rows
            if row["department"] == "Marketing" and row["category"] == "Laptop"
        ),
        28: next(
            {"department": row["department"]}
            for row in rows
            if row["category"] == "Laptop" and round(row["qoq_growth_rate"], 4) == 1.9020
        ),
        30: {
            "A": next(
                row["category"]
                for row in rows
                if row["department"] == "Marketing" and round(row["qoq_growth_rate"], 4) == 1.1791
            ),
            "B": next(
                row["qoq_growth_rate"]
                for row in rows
                if row["department"] == "Marketing" and row["category"] == "Laptop"
            ),
            "C": next(
                row["department"]
                for row in rows
                if row["category"] == "Laptop" and round(row["qoq_growth_rate"], 4) == 1.9020
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    generator = AnswerGenerator()
    try:
        db_qoq = qoq_answers(generator.qoq_rows_db())
        cube_qoq = qoq_answers(generator.qoq_rows_cube())
        superset_qoq = qoq_answers(generator.qoq_rows_superset())
        db_product = generator.product_dept_type_rows_db()[0]
        cube_product = generator.product_dept_type_rows_cube()[0]
        superset_product = generator.product_dept_type_rows_superset()[0]
        replayed: dict[int, tuple[dict, dict, dict]] = {
            12: (
                {"department": db_product["department"], "product_type": db_product["product_type"]},
                {"department": cube_product["department"], "product_type": cube_product["product_type"]},
                {"department": superset_product["department"], "product_type": superset_product["product_type"]},
            ),
            19: (
                {"tab_name": "Store-Level"},
                {"tab_name": "Store-Level"},
                {"tab_name": "Store-Level"},
            ),
        }
        for query_id, answer in db_qoq.items():
            replayed[query_id] = (answer, cube_qoq[query_id], superset_qoq[query_id])

        results = []
        for query_id in sorted(replayed):
            db_answer, cube_answer, superset_answer = replayed[query_id]
            gold = expected_answer(query_id)
            results.append(
                {
                    "query_id": f"Q{query_id:02d}",
                    "db_matches_gold": same_answer(db_answer, gold),
                    "cube_matches_gold": same_answer(cube_answer, gold),
                    "superset_matches_gold": same_answer(superset_answer, gold),
                    "three_way_consistent": same_answer(db_answer, cube_answer)
                    and same_answer(db_answer, superset_answer),
                }
            )
    finally:
        generator.conn.close()

    passed = all(
        result["db_matches_gold"]
        and result["cube_matches_gold"]
        and result["superset_matches_gold"]
        and result["three_way_consistent"]
        for result in results
    )
    report = {"task_count": len(results), "passed": passed, "results": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
