import json
import sys
import unittest
from pathlib import Path

current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent
if "" in sys.path:
    sys.path.remove("")
if str(project_root) in sys.path:
    sys.path.remove(str(project_root))
sys.path.append(str(project_root / "fastapi"))

from fastapi_service.cube import extract_join_map, summarize_schema  # noqa: E402
from fastapi_service.superset import QueryTranslater  # noqa: E402


class SupersetQueryTranslaterTestCase(unittest.TestCase):
    def test_summarize_schema(self) -> None:
        meta = {
            "cubes": [
                {
                    "name": "dim_product",
                    "dimensions": [
                        {"name": "dim_product.brand"},
                        {"name": "dim_product.product_key", "primaryKey": True},
                    ],
                    "measures": [{"name": "dim_product.total_receipts"}],
                }
            ]
        }
        summary = summarize_schema(meta)
        self.assertIn("dim_product", summary)
        self.assertIn("brand", summary["dim_product"]["columns"])

    def test_extract_join_map(self) -> None:
        meta = {
            "cubes": [
                {
                    "name": "fact_sales",
                    "joins": {
                        "dim_date": {
                            "sql": "{CUBE}.date_key = {dim_date}.date_key"
                        }
                    },
                }
            ]
        }
        joins = extract_join_map(meta)
        self.assertIn("fact_sales", joins)
        self.assertIn("dim_date", joins["fact_sales"])
        self.assertIn(("date_key", "date_key"), joins["fact_sales"]["dim_date"])

    def test_translate_payload_basic(self) -> None:
        def resolver(dataset_id: int) -> dict[str, object]:
            self.assertEqual(dataset_id, 27)
            return {
                "id": dataset_id,
                "table_name": "dim_product",
                "schema": None,
                "columns": ["dim_product_brand", "total_receipts"],
            }

        payload = {
            "datasource": {"id": 27, "type": "table"},
            "queries": [
                {
                    "columns": ["dim_product_brand"],
                    "metrics": [
                        {
                            "aggregate": "SUM",
                            "column": {"column_name": "total_receipts"},
                            "label": "SUM(total_receipts)",
                        }
                    ],
                    "filters": [
                        {
                            "col": "dim_date_date",
                            "op": "TEMPORAL_RANGE",
                            "val": "No filter",
                        }
                    ],
                    "row_limit": 5000,
                }
            ],
        }
        translator = QueryTranslater(dataset_resolver=resolver)
        parsed = translator.parse_payload(json.dumps(payload))
        self.assertIsNotNone(parsed)
        assert parsed is not None
        sql = parsed["sql"]
        self.assertIn("SELECT", sql)
        self.assertIn("SUM(total_receipts)", sql)
        self.assertIn("dim_product.brand", sql)
        self.assertIn("FROM dim_product", sql)
        self.assertIsInstance(parsed["filters"], list)

    def test_translate_payload_view(self) -> None:
        def resolver(dataset_id: int) -> dict[str, object]:
            self.assertEqual(dataset_id, 99)
            return {"table_name": "view_s1_marketing_qtr", "columns": []}

        translator = QueryTranslater(dataset_resolver=resolver)
        translator._cube_conf_cache = {
            "fact_sales": {
                "sql_table": "main.fact_sales",
                "columns": ["total_receipts", "product_key"],
                "joined": {
                    "dim_product": {
                        "joined_key": [
                            {
                                "from_table": "fact_sales",
                                "from_column": "product_key",
                                "to_table": "dim_product",
                                "to_column": "product_key",
                            }
                        ]
                    }
                },
            },
            "dim_product": {
                "sql_table": "main.dim_product",
                "columns": ["brand", "product_key"],
            },
            "view_s1_marketing_qtr": {
                "type": "view",
                "columns": ["dim_product_brand", "total_receipts"],
                "joined": {
                    "dim_product": {
                        "from": "fact_sales",
                        "to": "dim_product",
                        "joined_key": [
                            {
                                "from_table": "fact_sales",
                                "from_column": "product_key",
                                "to_table": "dim_product",
                                "to_column": "product_key",
                            }
                        ],
                    }
                },
            },
        }
        payload = {
            "datasource": {"id": 99, "type": "table"},
            "queries": [
                {
                    "columns": ["dim_product_brand"],
                    "metrics": [
                        {
                            "aggregate": "SUM",
                            "column": {"column_name": "total_receipts"},
                        }
                    ],
                }
            ],
        }
        parsed = translator.parse_payload(json.dumps(payload))
        self.assertIsNotNone(parsed)
        sql = parsed["sql"]
        self.assertIn("FROM main.fact_sales AS fact_sales", sql)
        self.assertIn("JOIN main.dim_product AS dim_product", sql)
        self.assertIn("dim_product.brand", sql)


if __name__ == "__main__":
    unittest.main()
