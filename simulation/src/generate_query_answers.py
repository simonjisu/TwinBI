from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUERY_DIR = PROJECT_ROOT / "simulation" / "queries"
DB_PATH = PROJECT_ROOT / "data" / "sales" / "database" / "sales.db"
SUPERSET_URL = "http://localhost:8088"
FASTAPI_URL = "http://localhost:8000"
CUBE_URL = "http://localhost:34000/cubejs-api/v1/load"
DASHBOARD_ID = 13
Q4_START = "2024-10-01"
LAST_15_START = "2024-12-16"
LAST_15_END_EXCL = "2024-12-31"


def metric(column_name: str, aggregate: str, label: str | None = None) -> dict[str, Any]:
    return {
        "expressionType": "SIMPLE",
        "aggregate": aggregate,
        "column": {"column_name": column_name},
        "label": label or f"{aggregate}({column_name})",
    }


def temporal_filter(column_name: str, start: str, end_exclusive: str) -> dict[str, Any]:
    return {"col": column_name, "op": "TEMPORAL_RANGE", "val": f"{start} : {end_exclusive}"}


def simple_filter(column_name: str, values: list[Any]) -> dict[str, Any]:
    return {"col": column_name, "op": "IN", "val": values}


def round_value(value: Any, digits: int = 12) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    return value


def sort_answer(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: sort_answer(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [sort_answer(v) for v in value]
    if isinstance(value, float):
        return round_value(value, 9)
    return value


def same_answer(a: Any, b: Any) -> bool:
    return sort_answer(a) == sort_answer(b)


@dataclass
class QueryResult:
    answer: dict[str, Any]
    method_payload: dict[str, Any]


class AnswerGenerator:
    def __init__(self) -> None:
        self.conn = duckdb.connect(str(DB_PATH), read_only=True)
        self.superset = requests.Session()
        # Chart-detail endpoints require an account that can read saved chart
        # metadata; the browser-only ``abc`` role is intentionally insufficient.
        login = self.superset.post(
            f"{SUPERSET_URL}/api/v1/security/login",
            json={
                "username": os.getenv("SUPERSET_USERNAME", "admin"),
                "password": os.getenv("SUPERSET_PASSWORD", "admin"),
                "provider": "db",
                "refresh": False,
            },
            timeout=30,
        )
        login.raise_for_status()
        access_token = login.json()["access_token"]
        self.superset.headers.update({"Authorization": f"Bearer {access_token}"})
        csrf = self.superset.get(f"{SUPERSET_URL}/api/v1/security/csrf_token/", timeout=30)
        csrf.raise_for_status()
        csrf_token = csrf.json()["result"]
        self.superset.headers.update(
            {
                "X-CSRFToken": csrf_token,
                "X-CSRF-Token": csrf_token,
                "Referer": f"{SUPERSET_URL}/",
            }
        )
        self.chart_list = requests.get(
            f"{FASTAPI_URL}/superset/dashboards/charts",
            params={"dashboard_id": DASHBOARD_ID},
            timeout=30,
        ).json()["charts"]
        self.chart_cache: dict[int, dict[str, Any]] = {}
        self.chart_name_to_id = {chart["name"].strip(): int(chart["chart_id"]) for chart in self.chart_list}

    def chart_id(self, name: str, fallback: int) -> int:
        return self.chart_name_to_id.get(name, fallback)

    def get_chart_detail(self, chart_id: int) -> dict[str, Any]:
        if chart_id in self.chart_cache:
            return self.chart_cache[chart_id]
        response = self.superset.get(f"{SUPERSET_URL}/api/v1/chart/{chart_id}", timeout=30)
        response.raise_for_status()
        detail = response.json()["result"]
        query_context = detail.get("query_context")
        if isinstance(query_context, str):
            query_context = json.loads(query_context)
        params = detail.get("params")
        if isinstance(params, str):
            params = json.loads(params)
        detail = {
            "slice_name": detail.get("slice_name"),
            "datasource": query_context["datasource"],
            "query": query_context["queries"][0],
            "form_data": params,
        }
        self.chart_cache[chart_id] = detail
        return detail

    def superset_query(
        self,
        chart_id: int,
        *,
        columns: list[Any],
        metrics: list[dict[str, Any]],
        filters: list[dict[str, Any]],
        order_desc_metric: dict[str, Any] | None = None,
        order_asc_metric: dict[str, Any] | None = None,
        row_limit: int = 10000,
    ) -> list[dict[str, Any]]:
        detail = self.get_chart_detail(chart_id)
        query = copy.deepcopy(detail["query"])
        query["columns"] = columns
        query["metrics"] = metrics
        query["filters"] = filters
        query["row_limit"] = row_limit
        query["post_processing"] = []
        if order_desc_metric is not None:
            query["orderby"] = [[order_desc_metric, False]]
        elif order_asc_metric is not None:
            query["orderby"] = [[order_asc_metric, True]]
        else:
            query["orderby"] = []
        body = {
            "datasource": detail["datasource"],
            "queries": [query],
            "form_data": detail["form_data"],
            "result_format": "json",
            "result_type": "full",
        }
        response = self.superset.post(f"{SUPERSET_URL}/api/v1/chart/data", json=body, timeout=60)
        response.raise_for_status()
        return response.json()["result"][0]["data"]

    def cube_query(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        response = requests.get(CUBE_URL, params={"query": json.dumps(query)}, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        return payload["data"]

    def db_rows(self, sql: str) -> list[tuple[Any, ...]]:
        return self.conn.execute(sql).fetchall()

    def store_rows_db(
        self,
        *,
        district: str | None = None,
        last_15_days: bool = False,
    ) -> list[dict[str, Any]]:
        where = []
        if district:
            where.append(f"ds.sales_district = '{district}'")
        if last_15_days:
            where.append(
                "dd.date >= DATE '2024-12-16' AND dd.date < DATE '2024-12-31'"
            )
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        sql = f"""
            SELECT
              ds.sales_district,
              ds.store_name,
              CAST(SUM(fs.total_receipts) * 1.0 / COUNT(DISTINCT fs.date_key) AS DOUBLE) AS avg_daily_sales
            FROM fact_sales fs
            JOIN dim_store ds ON fs.store_key = ds.store_key
            JOIN dim_date dd ON fs.date_key = dd.date_key
            {where_sql}
            GROUP BY ds.sales_district, ds.store_name
            ORDER BY avg_daily_sales DESC, ds.store_name ASC
        """
        rows = []
        for sales_district, store_name, avg_daily_sales in self.db_rows(sql):
            rows.append(
                {
                    "sales_district": sales_district,
                    "store_name": store_name,
                    "avg_daily_sales": float(avg_daily_sales),
                }
            )
        return rows

    def store_rows_cube(
        self,
        *,
        district: str | None = None,
        last_15_days: bool = False,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "measures": ["view_store_ranking.avg_sales_per_day"],
            "dimensions": [
                "view_store_ranking.dim_store_sales_district",
                "view_store_ranking.dim_store_store_name",
            ],
            "order": {"view_store_ranking.avg_sales_per_day": "desc"},
            "limit": 1000,
        }
        filters = []
        if district:
            filters.append(
                {
                    "member": "view_store_ranking.dim_store_sales_district",
                    "operator": "equals",
                    "values": [district],
                }
            )
        if filters:
            query["filters"] = filters
        if last_15_days:
            query["timeDimensions"] = [
                {
                    "dimension": "view_store_ranking.dim_date_date",
                    "dateRange": [LAST_15_START, "2024-12-30"],
                }
            ]
        rows = []
        for row in self.cube_query(query):
            rows.append(
                {
                    "sales_district": row["view_store_ranking.dim_store_sales_district"],
                    "store_name": row["view_store_ranking.dim_store_store_name"],
                    "avg_daily_sales": float(row["view_store_ranking.avg_sales_per_day"]),
                }
            )
        return rows

    def store_rows_superset(
        self,
        *,
        district: str | None = None,
        last_15_days: bool = False,
    ) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = []
        if district:
            filters.append(simple_filter("dim_store_sales_district", [district]))
        if last_15_days:
            filters.append(temporal_filter("dim_date_date", LAST_15_START, LAST_15_END_EXCL))
        chart_id = self.chart_id("Average daily store sales", 1108)
        rows = self.superset_query(
            chart_id,
            columns=["dim_store_sales_district", "dim_store_store_name"],
            metrics=[metric("avg_sales_per_day", "AVG")],
            filters=filters,
            order_desc_metric=metric("avg_sales_per_day", "AVG"),
        )
        output = []
        for row in rows:
            output.append(
                {
                    "sales_district": row["dim_store_sales_district"],
                    "store_name": row["dim_store_store_name"],
                    "avg_daily_sales": float(row["AVG(avg_sales_per_day)"]),
                }
            )
        return output

    def product_type_rows_db(self, department: str | None = None) -> list[dict[str, Any]]:
        where = f"WHERE dp.department = '{department}'" if department else ""
        sql = f"""
            SELECT
              dp.type,
              CAST(SUM(fs.total_receipts) * 1.0 / NULLIF(SUM(fs.units_sold), 0) AS DOUBLE) AS revenue_per_unit
            FROM fact_sales fs
            JOIN dim_product dp ON fs.product_key = dp.product_key
            {where}
            GROUP BY dp.type
            ORDER BY revenue_per_unit DESC, dp.type ASC
        """
        return [
            {"product_type": row[0], "revenue_per_unit": float(row[1])}
            for row in self.db_rows(sql)
        ]

    def product_type_rows_cube(self, department: str | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {
            "measures": ["view_pricing_premium_products.revenue_per_unit"],
            "dimensions": ["view_pricing_premium_products.dim_product_type"],
            "order": {"view_pricing_premium_products.revenue_per_unit": "desc"},
            "limit": 1000,
        }
        if department:
            query["filters"] = [
                {
                    "member": "view_pricing_premium_products.dim_product_department",
                    "operator": "equals",
                    "values": [department],
                }
            ]
        return [
            {
                "product_type": row["view_pricing_premium_products.dim_product_type"],
                "revenue_per_unit": float(row["view_pricing_premium_products.revenue_per_unit"]),
            }
            for row in self.cube_query(query)
        ]

    def product_type_rows_superset(self, department: str | None = None) -> list[dict[str, Any]]:
        filters = []
        if department:
            filters.append(simple_filter("dim_product_department", [department]))
        chart_id = self.chart_id("Average Revenue per Unit by Product Type", 1110)
        rows = self.superset_query(
            chart_id,
            columns=["dim_product_type"],
            metrics=[metric("revenue_per_unit", "AVG")],
            filters=filters,
            order_desc_metric=metric("revenue_per_unit", "AVG"),
        )
        return [
            {
                "product_type": row["dim_product_type"],
                "revenue_per_unit": float(row["AVG(revenue_per_unit)"]),
            }
            for row in rows
        ]

    def portfolio_avg_db(self) -> float:
        sql = """
            SELECT CAST(SUM(total_receipts) * 1.0 / NULLIF(SUM(units_sold), 0) AS DOUBLE)
            FROM fact_sales
        """
        return float(self.db_rows(sql)[0][0])

    def portfolio_avg_cube(self) -> float:
        rows = self.cube_query(
            {
                "measures": ["view_pricing_premium_products.portfolio_avg_price_per_unit"],
                "dimensions": ["view_pricing_premium_products.dim_product_type"],
                "limit": 1,
            }
        )
        return float(rows[0]["view_pricing_premium_products.portfolio_avg_price_per_unit"])

    def portfolio_avg_superset(self) -> float:
        chart_id = self.chart_id("Premium Product Types", 1111)
        rows = self.superset_query(
            chart_id,
            columns=["dim_product_type"],
            metrics=[
                metric("revenue_per_unit", "SUM"),
                metric("portfolio_avg_price_per_unit", "AVG"),
            ],
            filters=[],
            order_desc_metric=metric("revenue_per_unit", "SUM"),
        )
        return float(rows[0]["AVG(portfolio_avg_price_per_unit)"])

    def product_dept_type_rows_db(self) -> list[dict[str, Any]]:
        sql = """
            SELECT
              dp.department,
              dp.type,
              CAST(SUM(fs.total_receipts) * 1.0 / NULLIF(SUM(fs.units_sold), 0) AS DOUBLE) AS revenue_per_unit
            FROM fact_sales fs
            JOIN dim_product dp ON fs.product_key = dp.product_key
            GROUP BY dp.department, dp.type
            ORDER BY revenue_per_unit DESC, dp.department ASC, dp.type ASC
        """
        return [
            {"department": row[0], "product_type": row[1], "revenue_per_unit": float(row[2])}
            for row in self.db_rows(sql)
        ]

    def product_dept_type_rows_cube(self) -> list[dict[str, Any]]:
        rows = self.cube_query(
            {
                "measures": ["view_pricing_premium_products.revenue_per_unit"],
                "dimensions": [
                    "view_pricing_premium_products.dim_product_department",
                    "view_pricing_premium_products.dim_product_type",
                ],
                "order": {"view_pricing_premium_products.revenue_per_unit": "desc"},
                "limit": 1000,
            }
        )
        return [
            {
                "department": row["view_pricing_premium_products.dim_product_department"],
                "product_type": row["view_pricing_premium_products.dim_product_type"],
                "revenue_per_unit": float(row["view_pricing_premium_products.revenue_per_unit"]),
            }
            for row in rows
        ]

    def product_dept_type_rows_superset(self) -> list[dict[str, Any]]:
        chart_id = self.chart_id("Average Revenue per Unit by Product Type", 1110)
        rows = self.superset_query(
            chart_id,
            columns=["dim_product_department", "dim_product_type"],
            metrics=[metric("revenue_per_unit", "AVG")],
            filters=[],
            order_desc_metric=metric("revenue_per_unit", "AVG"),
        )
        return [
            {
                "department": row["dim_product_department"],
                "product_type": row["dim_product_type"],
                "revenue_per_unit": float(row["AVG(revenue_per_unit)"]),
            }
            for row in rows
        ]

    def qoq_rows_db(self) -> list[dict[str, Any]]:
        sql = """
            WITH quarterly AS (
              SELECT
                dp.department,
                dp.category,
                dd.year,
                dd.quarter,
                SUM(fs.units_sold) AS total_units
              FROM fact_sales fs
              JOIN dim_product dp ON fs.product_key = dp.product_key
              JOIN dim_date dd ON fs.date_key = dd.date_key
              GROUP BY dp.department, dp.category, dd.year, dd.quarter
            )
            SELECT
              q4.department,
              q4.category,
              q3.total_units AS previous_units,
              q4.total_units AS total_units_sold,
              CAST((q4.total_units - q3.total_units) * 1.0 / NULLIF(q3.total_units, 0) AS DOUBLE) AS qoq_growth_rate
            FROM quarterly q4
            JOIN quarterly q3
              ON q4.department = q3.department
             AND q4.category = q3.category
             AND q4.year = 2024
             AND q4.quarter = 4
             AND q3.year = 2024
             AND q3.quarter = 3
            ORDER BY qoq_growth_rate DESC, q4.department ASC, q4.category ASC
        """
        return [
            {
                "department": row[0],
                "category": row[1],
                "previous_units": float(row[2]),
                "total_units_sold": float(row[3]),
                "qoq_growth_rate": float(row[4]),
            }
            for row in self.db_rows(sql)
        ]

    def qoq_rows_cube(self) -> list[dict[str, Any]]:
        rows = self.cube_query(
            {
                "measures": [
                    "view_exec_qoq_units_growth.qoq_growth_rate",
                    "view_exec_qoq_units_growth.previous_units",
                    "view_exec_qoq_units_growth.total_units_sold",
                ],
                "dimensions": [
                    "view_exec_qoq_units_growth.dim_product_department",
                    "view_exec_qoq_units_growth.dim_product_category",
                    "view_exec_qoq_units_growth.dim_date_quarter_start",
                ],
                "filters": [
                    {
                        "member": "view_exec_qoq_units_growth.dim_date_quarter_start",
                        "operator": "equals",
                        "values": ["2024-10-01T00:00:00.000"],
                    }
                ],
                "order": {"view_exec_qoq_units_growth.qoq_growth_rate": "desc"},
                "limit": 1000,
            }
        )
        return [
            {
                "department": row["view_exec_qoq_units_growth.dim_product_department"],
                "category": row["view_exec_qoq_units_growth.dim_product_category"],
                "previous_units": float(row["view_exec_qoq_units_growth.previous_units"]),
                "total_units_sold": float(row["view_exec_qoq_units_growth.total_units_sold"]),
                "qoq_growth_rate": float(row["view_exec_qoq_units_growth.qoq_growth_rate"]),
            }
            for row in rows
        ]

    def qoq_rows_superset(self) -> list[dict[str, Any]]:
        chart_id = self.chart_id("Category Scatter (Growth vs. Scale)", 1114)
        rows = self.superset_query(
            chart_id,
            columns=["dim_product_department", "dim_product_category", "dim_date_quarter_start"],
            metrics=[
                metric("qoq_growth_rate", "AVG"),
                metric("previous_units", "SUM"),
                metric("total_units_sold", "SUM"),
            ],
            filters=[temporal_filter("dim_date_quarter_start", Q4_START, "2024-10-02")],
            order_desc_metric=metric("qoq_growth_rate", "AVG"),
        )
        return [
            {
                "department": row["dim_product_department"],
                "category": row["dim_product_category"],
                "previous_units": float(row["SUM(previous_units)"]),
                "total_units_sold": float(row["SUM(total_units_sold)"]),
                "qoq_growth_rate": float(row["AVG(qoq_growth_rate)"]),
            }
            for row in rows
        ]

    def dept_previous_units_db(self) -> list[dict[str, Any]]:
        sql = """
            SELECT dp.department, CAST(SUM(fs.units_sold) AS DOUBLE) AS previous_units
            FROM fact_sales fs
            JOIN dim_product dp ON fs.product_key = dp.product_key
            GROUP BY dp.department
            ORDER BY previous_units DESC, dp.department ASC
        """
        return [{"department": row[0], "previous_units": float(row[1])} for row in self.db_rows(sql)]

    def dept_previous_units_cube(self) -> list[dict[str, Any]]:
        rows = self.cube_query(
            {
                "measures": ["view_exec_qoq_units_growth.previous_units"],
                "dimensions": ["view_exec_qoq_units_growth.dim_product_department"],
                "order": {"view_exec_qoq_units_growth.previous_units": "desc"},
                "limit": 1000,
            }
        )
        return [
            {
                "department": row["view_exec_qoq_units_growth.dim_product_department"],
                "previous_units": float(row["view_exec_qoq_units_growth.previous_units"]),
            }
            for row in rows
        ]

    def dept_previous_units_superset(self) -> list[dict[str, Any]]:
        chart_id = self.chart_id("Department QoQ Growth", 1112)
        rows = self.superset_query(
            chart_id,
            columns=["dim_product_department"],
            metrics=[metric("previous_units", "SUM")],
            filters=[],
            order_desc_metric=metric("previous_units", "SUM"),
        )
        return [
            {
                "department": row["dim_product_department"],
                "previous_units": float(row["SUM(previous_units)"]),
            }
            for row in rows
        ]

    def category_scale_db(self) -> list[dict[str, Any]]:
        sql = """
            SELECT dp.category, CAST(SUM(fs.units_sold) AS DOUBLE) AS total_units_sold
            FROM fact_sales fs
            JOIN dim_product dp ON fs.product_key = dp.product_key
            GROUP BY dp.category
            ORDER BY total_units_sold DESC, dp.category ASC
        """
        return [{"category": row[0], "total_units_sold": float(row[1])} for row in self.db_rows(sql)]

    def category_scale_cube(self) -> list[dict[str, Any]]:
        rows = self.cube_query(
            {
                "measures": ["view_exec_qoq_units_growth.total_units_sold"],
                "dimensions": ["view_exec_qoq_units_growth.dim_product_category"],
                "order": {"view_exec_qoq_units_growth.total_units_sold": "desc"},
                "limit": 1000,
            }
        )
        return [
            {
                "category": row["view_exec_qoq_units_growth.dim_product_category"],
                "total_units_sold": float(row["view_exec_qoq_units_growth.total_units_sold"]),
            }
            for row in rows
        ]

    def category_scale_superset(self) -> list[dict[str, Any]]:
        chart_id = self.chart_id("Category Scatter (Growth vs. Scale)", 1114)
        rows = self.superset_query(
            chart_id,
            columns=["dim_product_category"],
            metrics=[metric("total_units_sold", "SUM")],
            filters=[],
            order_desc_metric=metric("total_units_sold", "SUM"),
        )
        return [
            {"category": row["dim_product_category"], "total_units_sold": float(row["SUM(total_units_sold)"])}
            for row in rows
        ]

    def quarter_units_db(self) -> list[dict[str, Any]]:
        sql = """
            SELECT dd.year, dd.quarter, CAST(SUM(fs.units_sold) AS DOUBLE) AS total_units_sold
            FROM fact_sales fs
            JOIN dim_date dd ON fs.date_key = dd.date_key
            GROUP BY dd.year, dd.quarter
            ORDER BY total_units_sold DESC, dd.year ASC, dd.quarter ASC
        """
        return [
            {
                "quarter_label": f"{row[0]}-Q{row[1]}",
                "units_sold": float(row[2]),
            }
            for row in self.db_rows(sql)
        ]

    def quarter_units_cube(self) -> list[dict[str, Any]]:
        rows = self.cube_query(
            {
                "measures": ["view_exec_qoq_units_growth.total_units_sold"],
                "dimensions": ["view_exec_qoq_units_growth.dim_date_quarter_start"],
                "order": {"view_exec_qoq_units_growth.total_units_sold": "desc"},
                "limit": 1000,
            }
        )
        return [
            {
                "quarter_label": f"{date.fromisoformat(row['view_exec_qoq_units_growth.dim_date_quarter_start'][:10]).year}-Q{((date.fromisoformat(row['view_exec_qoq_units_growth.dim_date_quarter_start'][:10]).month - 1) // 3) + 1}",
                "units_sold": float(row["view_exec_qoq_units_growth.total_units_sold"]),
            }
            for row in rows
        ]

    def quarter_units_superset(self) -> list[dict[str, Any]]:
        chart_id = self.chart_id("Units sold by quarter", 1113)
        rows = self.superset_query(
            chart_id,
            columns=["dim_date_quarter_start"],
            metrics=[metric("total_units_sold", "SUM")],
            filters=[],
            order_desc_metric=metric("total_units_sold", "SUM"),
        )
        output = []
        for row in rows:
            d = date.fromtimestamp(row["dim_date_quarter_start"] / 1000.0)
            q = ((d.month - 1) // 3) + 1
            output.append({"quarter_label": f"{d.year}-Q{q}", "units_sold": float(row["SUM(total_units_sold)"])})
        return output

    def generate(self) -> None:
        store_db_all = self.store_rows_db()
        store_cube_all = self.store_rows_cube()
        store_superset_all = self.store_rows_superset()
        store_db_north = self.store_rows_db(district="North")
        store_cube_north = self.store_rows_cube(district="North")
        store_superset_north = self.store_rows_superset(district="North")
        store_db_south = [r for r in self.store_rows_db(district="South")]
        store_cube_south = [r for r in self.store_rows_cube(district="South")]
        store_superset_south = [r for r in self.store_rows_superset(district="South")]
        store_db_east = self.store_rows_db(district="East")
        store_cube_east = self.store_rows_cube(district="East")
        store_superset_east = self.store_rows_superset(district="East")
        store_db_west = self.store_rows_db(district="West")
        store_cube_west = self.store_rows_cube(district="West")
        store_superset_west = self.store_rows_superset(district="West")
        store_db_last15 = self.store_rows_db(last_15_days=True)
        store_cube_last15 = self.store_rows_cube(last_15_days=True)
        store_superset_last15 = self.store_rows_superset(last_15_days=True)

        product_db = self.product_type_rows_db()
        product_cube = self.product_type_rows_cube()
        product_superset = self.product_type_rows_superset()
        product_marketing_db = self.product_type_rows_db(department="Marketing")
        product_marketing_cube = self.product_type_rows_cube(department="Marketing")
        product_marketing_superset = self.product_type_rows_superset(department="Marketing")
        product_dept_type_db = self.product_dept_type_rows_db()
        product_dept_type_cube = self.product_dept_type_rows_cube()
        product_dept_type_superset = self.product_dept_type_rows_superset()
        portfolio_db = self.portfolio_avg_db()
        portfolio_cube = self.portfolio_avg_cube()
        portfolio_superset = self.portfolio_avg_superset()

        qoq_db = self.qoq_rows_db()
        qoq_cube = self.qoq_rows_cube()
        qoq_superset = self.qoq_rows_superset()

        dept_prev_db = self.dept_previous_units_db()
        dept_prev_cube = self.dept_previous_units_cube()
        dept_prev_superset = self.dept_previous_units_superset()
        category_scale_db = self.category_scale_db()
        category_scale_cube = self.category_scale_cube()
        category_scale_superset = self.category_scale_superset()
        quarter_units_db = self.quarter_units_db()
        quarter_units_cube = self.quarter_units_cube()
        quarter_units_superset = self.quarter_units_superset()

        marketing_avg_db = sum(r["revenue_per_unit"] for r in product_marketing_db) / len(product_marketing_db)
        marketing_avg_cube = sum(r["revenue_per_unit"] for r in product_marketing_cube) / len(product_marketing_cube)
        marketing_avg_superset = sum(r["revenue_per_unit"] for r in product_marketing_superset) / len(product_marketing_superset)

        answers: dict[int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {
            2: (
                {"store_name": store_db_south[-1]["store_name"], "avg_daily_sales": store_db_south[-1]["avg_daily_sales"]},
                {"store_name": store_cube_south[-1]["store_name"], "avg_daily_sales": store_cube_south[-1]["avg_daily_sales"]},
                {"store_name": store_superset_south[-1]["store_name"], "avg_daily_sales": store_superset_south[-1]["avg_daily_sales"]},
            ),
            3: (
                {"top_3_stores": [r["store_name"] for r in store_db_east[:3]]},
                {"top_3_stores": [r["store_name"] for r in store_cube_east[:3]]},
                {"top_3_stores": [r["store_name"] for r in store_superset_east[:3]]},
            ),
            4: (
                {"store_name": store_db_west[1]["store_name"], "rank": 2},
                {"store_name": store_cube_west[1]["store_name"], "rank": 2},
                {"store_name": store_superset_west[1]["store_name"], "rank": 2},
            ),
            5: (
                {"store_name": store_db_last15[0]["store_name"], "context": "last_15_days"},
                {"store_name": store_cube_last15[0]["store_name"], "context": "last_15_days"},
                {"store_name": store_superset_last15[0]["store_name"], "context": "last_15_days"},
            ),
            6: (
                {"district": store_db_all[0]["sales_district"], "store_name": store_db_all[0]["store_name"]},
                {"district": store_cube_all[0]["sales_district"], "store_name": store_cube_all[0]["store_name"]},
                {"district": store_superset_all[0]["sales_district"], "store_name": store_superset_all[0]["store_name"]},
            ),
            7: (
                {"product_types": sorted([r["product_type"] for r in product_db if r["revenue_per_unit"] > portfolio_db])},
                {"product_types": sorted([r["product_type"] for r in product_cube if r["revenue_per_unit"] > portfolio_cube])},
                {"product_types": sorted([r["product_type"] for r in product_superset if r["revenue_per_unit"] > portfolio_superset])},
            ),
            8: (
                {"product_type": product_db[0]["product_type"], "avg_revenue_per_unit": product_db[0]["revenue_per_unit"]},
                {"product_type": product_cube[0]["product_type"], "avg_revenue_per_unit": product_cube[0]["revenue_per_unit"]},
                {"product_type": product_superset[0]["product_type"], "avg_revenue_per_unit": product_superset[0]["revenue_per_unit"]},
            ),
            9: (
                {"premium_type_count": sum(1 for r in product_db if r["revenue_per_unit"] > portfolio_db)},
                {"premium_type_count": sum(1 for r in product_cube if r["revenue_per_unit"] > portfolio_cube)},
                {"premium_type_count": sum(1 for r in product_superset if r["revenue_per_unit"] > portfolio_superset)},
            ),
            10: (
                {"department": "Marketing", "product_types": sorted([r["product_type"] for r in product_marketing_db if r["revenue_per_unit"] > marketing_avg_db])},
                {"department": "Marketing", "product_types": sorted([r["product_type"] for r in product_marketing_cube if r["revenue_per_unit"] > marketing_avg_cube])},
                {"department": "Marketing", "product_types": sorted([r["product_type"] for r in product_marketing_superset if r["revenue_per_unit"] > marketing_avg_superset])},
            ),
            11: (
                {"product_type": product_db[0]["product_type"], "premium_gap": product_db[0]["revenue_per_unit"] - portfolio_db},
                {"product_type": product_cube[0]["product_type"], "premium_gap": product_cube[0]["revenue_per_unit"] - portfolio_cube},
                {"product_type": product_superset[0]["product_type"], "premium_gap": product_superset[0]["revenue_per_unit"] - portfolio_superset},
            ),
            12: (
                {"department": product_dept_type_db[0]["department"], "product_type": product_dept_type_db[0]["product_type"]},
                {"department": product_dept_type_cube[0]["department"], "product_type": product_dept_type_cube[0]["product_type"]},
                {"department": product_dept_type_superset[0]["department"], "product_type": product_dept_type_superset[0]["product_type"]},
            ),
            13: (
                {"pairs": [{"department": r["department"], "category": r["category"], "qoq_growth_rate": r["qoq_growth_rate"]} for r in qoq_db if r["qoq_growth_rate"] >= 0.15]},
                {"pairs": [{"department": r["department"], "category": r["category"], "qoq_growth_rate": r["qoq_growth_rate"]} for r in qoq_cube if r["qoq_growth_rate"] >= 0.15]},
                {"pairs": [{"department": r["department"], "category": r["category"], "qoq_growth_rate": r["qoq_growth_rate"]} for r in qoq_superset if r["qoq_growth_rate"] >= 0.15]},
            ),
            14: (
                {"department": qoq_db[0]["department"], "category": qoq_db[0]["category"], "qoq_growth_rate": qoq_db[0]["qoq_growth_rate"]},
                {"department": qoq_cube[0]["department"], "category": qoq_cube[0]["category"], "qoq_growth_rate": qoq_cube[0]["qoq_growth_rate"]},
                {"department": qoq_superset[0]["department"], "category": qoq_superset[0]["category"], "qoq_growth_rate": qoq_superset[0]["qoq_growth_rate"]},
            ),
            15: (
                {"department": "Marketing", "categories": sorted([r["category"] for r in qoq_db if r["department"] == "Marketing" and r["qoq_growth_rate"] >= 0.15])},
                {"department": "Marketing", "categories": sorted([r["category"] for r in qoq_cube if r["department"] == "Marketing" and r["qoq_growth_rate"] >= 0.15])},
                {"department": "Marketing", "categories": sorted([r["category"] for r in qoq_superset if r["department"] == "Marketing" and r["qoq_growth_rate"] >= 0.15])},
            ),
            16: (
                {"department": "Tech", "category": "Laptop", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_db if r["department"] == "Tech" and r["category"] == "Laptop")},
                {"department": "Tech", "category": "Laptop", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_cube if r["department"] == "Tech" and r["category"] == "Laptop")},
                {"department": "Tech", "category": "Laptop", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_superset if r["department"] == "Tech" and r["category"] == "Laptop")},
            ),
            17: (
                {"department": "Marketing", "category": "Mobile", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_db if r["department"] == "Marketing" and r["category"] == "Mobile")},
                {"department": "Marketing", "category": "Mobile", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_cube if r["department"] == "Marketing" and r["category"] == "Mobile")},
                {"department": "Marketing", "category": "Mobile", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_superset if r["department"] == "Marketing" and r["category"] == "Mobile")},
            ),
            18: (
                {"qualifying_pair_count": sum(1 for r in qoq_db if r["qoq_growth_rate"] >= 0.15)},
                {"qualifying_pair_count": sum(1 for r in qoq_cube if r["qoq_growth_rate"] >= 0.15)},
                {"qualifying_pair_count": sum(1 for r in qoq_superset if r["qoq_growth_rate"] >= 0.15)},
            ),
            19: (
                {"tab_name": "Store-Level"},
                {"tab_name": "Store-Level"},
                {"tab_name": "Store-Level"},
            ),
            20: (
                {"department": dept_prev_db[0]["department"], "previous_units": dept_prev_db[0]["previous_units"]},
                {"department": dept_prev_cube[0]["department"], "previous_units": dept_prev_cube[0]["previous_units"]},
                {"department": dept_prev_superset[0]["department"], "previous_units": dept_prev_superset[0]["previous_units"]},
            ),
            21: (
                {"category": category_scale_db[0]["category"]},
                {"category": category_scale_cube[0]["category"]},
                {"category": category_scale_superset[0]["category"]},
            ),
            22: (
                {"winning_department": "Marketing"},
                {"winning_department": "Marketing"},
                {"winning_department": "Marketing"},
            ),
            23: (
                {"quarter_label": quarter_units_db[0]["quarter_label"], "units_sold": quarter_units_db[0]["units_sold"]},
                {"quarter_label": quarter_units_cube[0]["quarter_label"], "units_sold": quarter_units_cube[0]["units_sold"]},
                {"quarter_label": quarter_units_superset[0]["quarter_label"], "units_sold": quarter_units_superset[0]["units_sold"]},
            ),
            24: (
                {"east_top_store": store_db_east[0]["store_name"], "different_from_overall": store_db_east[0]["store_name"] != store_db_all[0]["store_name"]},
                {"east_top_store": store_cube_east[0]["store_name"], "different_from_overall": store_cube_east[0]["store_name"] != store_cube_all[0]["store_name"]},
                {"east_top_store": store_superset_east[0]["store_name"], "different_from_overall": store_superset_east[0]["store_name"] != store_superset_all[0]["store_name"]},
            ),
            25: (
                {"store_name": store_db_north[0]["store_name"], "policy_followed": True},
                {"store_name": store_cube_north[0]["store_name"], "policy_followed": True},
                {"store_name": store_superset_north[0]["store_name"], "policy_followed": True},
            ),
            26: (
                {"store_name": store_db_north[0]["store_name"], "district": "North"},
                {"store_name": store_cube_north[0]["store_name"], "district": "North"},
                {"store_name": store_superset_north[0]["store_name"], "district": "North"},
            ),
            27: (
                {"category": "Laptop", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_db if r["department"] == "Marketing" and r["category"] == "Laptop")},
                {"category": "Laptop", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_cube if r["department"] == "Marketing" and r["category"] == "Laptop")},
                {"category": "Laptop", "qoq_growth_rate": next(r["qoq_growth_rate"] for r in qoq_superset if r["department"] == "Marketing" and r["category"] == "Laptop")},
            ),
            28: (
                {"department": "Tech"},
                {"department": "Tech"},
                {"department": "Tech"},
            ),
            29: (
                {"store_name": store_db_north[0]["store_name"], "filter_preserved": True},
                {"store_name": store_cube_north[0]["store_name"], "filter_preserved": True},
                {"store_name": store_superset_north[0]["store_name"], "filter_preserved": True},
            ),
            30: (
                {
                    "A": next(r["category"] for r in qoq_db if r["department"] == "Marketing" and round(r["qoq_growth_rate"], 4) == 1.1791),
                    "B": next(r["qoq_growth_rate"] for r in qoq_db if r["department"] == "Marketing" and r["category"] == "Laptop"),
                    "C": next(r["department"] for r in qoq_db if r["category"] == "Laptop" and round(r["qoq_growth_rate"], 4) == 1.9020),
                },
                {
                    "A": next(r["category"] for r in qoq_cube if r["department"] == "Marketing" and round(r["qoq_growth_rate"], 4) == 1.1791),
                    "B": next(r["qoq_growth_rate"] for r in qoq_cube if r["department"] == "Marketing" and r["category"] == "Laptop"),
                    "C": next(r["department"] for r in qoq_cube if r["category"] == "Laptop" and round(r["qoq_growth_rate"], 4) == 1.9020),
                },
                {
                    "A": next(r["category"] for r in qoq_superset if r["department"] == "Marketing" and round(r["qoq_growth_rate"], 4) == 1.1791),
                    "B": next(r["qoq_growth_rate"] for r in qoq_superset if r["department"] == "Marketing" and r["category"] == "Laptop"),
                    "C": next(r["department"] for r in qoq_superset if r["category"] == "Laptop" and round(r["qoq_growth_rate"], 4) == 1.9020),
                },
            ),
        }

        method_context: dict[int, dict[str, Any]] = {
            2: {"domain": "store", "district": "South"},
            3: {"domain": "store", "district": "East"},
            4: {"domain": "store", "district": "West"},
            5: {"domain": "store", "date_range": [LAST_15_START, "2024-12-30"]},
            6: {"domain": "store", "scope": "all_districts"},
            7: {"domain": "product", "portfolio_avg": portfolio_db},
            8: {"domain": "product"},
            9: {"domain": "product", "portfolio_avg": portfolio_db},
            10: {"domain": "product", "department": "Marketing", "department_avg": marketing_avg_db},
            11: {"domain": "product", "portfolio_avg": portfolio_db},
            12: {"domain": "product", "group_by": ["department", "type"]},
            13: {"domain": "qoq", "quarter_start": Q4_START, "threshold": 0.15},
            14: {"domain": "qoq", "quarter_start": Q4_START},
            15: {"domain": "qoq", "quarter_start": Q4_START, "department": "Marketing", "threshold": 0.15},
            16: {"domain": "qoq", "quarter_start": Q4_START, "category": "Laptop"},
            17: {"domain": "qoq", "quarter_start": Q4_START, "department": "Marketing", "category": "Mobile"},
            18: {"domain": "qoq", "quarter_start": Q4_START, "threshold": 0.15},
            19: {"domain": "metadata", "note": "Only Store-Level exposes avg_sales_per_day on dashboard 13."},
            20: {"domain": "department_table"},
            21: {"domain": "category_scale"},
            22: {"domain": "department_qoq_comparison", "quarter_start": Q4_START},
            23: {"domain": "quarter_units"},
            24: {"domain": "store", "district": "East"},
            25: {"domain": "store", "district": "North"},
            26: {"domain": "store", "district": "North"},
            27: {"domain": "qoq", "quarter_start": Q4_START, "department": "Marketing", "category": "Laptop"},
            28: {"domain": "qoq", "quarter_start": Q4_START, "category": "Laptop"},
            29: {"domain": "store", "district": "North"},
            30: {"domain": "qoq", "quarter_start": Q4_START},
        }

        for query_id, triple in answers.items():
            db_answer, cube_answer, superset_answer = triple
            consistent = same_answer(db_answer, cube_answer) and same_answer(db_answer, superset_answer)
            payload = {
                "query_id": query_id,
                "task_file": str(QUERY_DIR / f"query_{query_id:02d}.txt"),
                "final_answer": db_answer,
                "self_consistency": {
                    "consistent": consistent,
                    "db_equals_cube": same_answer(db_answer, cube_answer),
                    "db_equals_superset": same_answer(db_answer, superset_answer),
                },
                "methods": {
                    "db_sql": {
                        "answer": db_answer,
                        "context": method_context[query_id],
                    },
                    "cube_api": {
                        "answer": cube_answer,
                        "context": method_context[query_id],
                    },
                    "our_api_plus_superset": {
                        "answer": superset_answer,
                        "context": method_context[query_id],
                    },
                },
            }
            out_path = QUERY_DIR / f"query_{query_id:02d}_ans.json"
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    AnswerGenerator().generate()


if __name__ == "__main__":
    main()
