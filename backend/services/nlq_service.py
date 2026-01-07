from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from backend.api.schemas import (
    CubeFilter,
    CubeQuery,
    NLQPlanRequest,
    NLQPlanResponse,
    SelectionPayload,
    TimeDimension,
)
from backend.core.config import settings


@dataclass
class NLQIntent:
    measure: str
    dimension: Optional[str]
    time_dimension: Optional[str]
    granularity: Optional[str]
    chart_type: str
    reasoning: str


class NLQService:
    """Lightweight rule-based NLQ parser to produce Cube queries."""

    MEASURE_KEYWORDS = {
        "sales": "fact_sales.total_sales_amount",
        "revenue": "fact_sales.total_sales_amount",
        "units": "fact_sales.total_units_sold",
        "orders": "fact_sales.total_transactions",
    }

    DIMENSION_KEYWORDS = {
        "category": "dim_product.category",
        "product": "dim_product.product_name",
        "department": "dim_product.department",
        "store": "dim_store.store_name",
        "state": "dim_store.state",
        "city": "dim_store.city",
        "manager": "dim_store.sales_manager",
    }

    def __init__(self) -> None:
        self.settings = settings

    def build_plan(self, request: NLQPlanRequest) -> Tuple[NLQPlanResponse, NLQIntent]:
        intent = self._parse_intent(request.message)
        filters = list(request.active_filters or [])
        if request.last_selection and request.last_selection.derived_filters:
            filters.extend(request.last_selection.derived_filters)

        time_dimensions: List[TimeDimension] = []
        if intent.time_dimension:
            date_range = self._infer_date_range(request.message)
            time_dimensions.append(
                TimeDimension(
                    dimension=intent.time_dimension,
                    granularity=intent.granularity or "day",
                    dateRange=date_range,
                )
            )

        cube_query = CubeQuery(
            measures=[intent.measure],
            dimensions=[intent.dimension] if intent.dimension else [],
            timeDimensions=time_dimensions,
            filters=filters,
            order={},
            limit=50,
        )

        if intent.time_dimension:
            cube_query.order[intent.time_dimension] = "asc"
        elif intent.dimension:
            cube_query.order[intent.dimension] = "desc"

        plan_steps = [
            "Parse NLQ for measure, dimensions, and time context",
            "Merge active filters and chart selections",
            "Generate Cube query and choose visualization type",
        ]

        response = NLQPlanResponse(
            plan=plan_steps,
            cube_query=cube_query,
            suggested_chart=intent.chart_type,
            reasoning=intent.reasoning,
        )
        return response, intent

    def _parse_intent(self, message: str) -> NLQIntent:
        lowered = message.lower()
        measure = self._infer_measure(lowered)
        dimension = self._infer_dimension(lowered)
        time_dimension, granularity = self._infer_time_dimension(lowered)
        chart_type = self._infer_chart_type(lowered, time_dimension, dimension)
        reasoning = (
            f"Using measure '{measure}' with dimension "
            f"'{dimension or 'none'}' and time '{time_dimension or 'none'}'."
        )
        return NLQIntent(
            measure=measure,
            dimension=dimension,
            time_dimension=time_dimension,
            granularity=granularity,
            chart_type=chart_type,
            reasoning=reasoning,
        )

    def _infer_measure(self, lowered: str) -> str:
        for keyword, measure in self.MEASURE_KEYWORDS.items():
            if keyword in lowered:
                return measure
        return "fact_sales.total_sales_amount"

    def _infer_dimension(self, lowered: str) -> Optional[str]:
        for keyword, dimension in self.DIMENSION_KEYWORDS.items():
            if keyword in lowered:
                return dimension
        return None

    def _infer_time_dimension(self, lowered: str) -> Tuple[Optional[str], Optional[str]]:
        if "week" in lowered:
            return "dim_date.date", "week"
        if "month" in lowered:
            return "dim_date.date", "month"
        if "quarter" in lowered:
            return "dim_date.date", "quarter"
        if "year" in lowered:
            return "dim_date.date", "year"
        if "day" in lowered or "today" in lowered or "yesterday" in lowered:
            return "dim_date.date", "day"
        return None, None

    def _infer_date_range(self, message: str) -> List[str]:
        lowered = message.lower()
        window_match = re.search(r"last\s+(\d+)\s+(day|days|week|weeks|month|months)", lowered)
        if window_match:
            value = int(window_match.group(1))
            unit = window_match.group(2)
            delta = timedelta(days=value)
            if "week" in unit:
                delta = timedelta(weeks=value)
            elif "month" in unit:
                delta = timedelta(days=value * 30)
            end_date = datetime.utcnow().date()
            start_date = end_date - delta
            return [start_date.isoformat(), end_date.isoformat()]
        return [
            (datetime.utcnow() - timedelta(days=30)).date().isoformat(),
            datetime.utcnow().date().isoformat(),
        ]

    def _infer_chart_type(
        self, lowered: str, time_dimension: Optional[str], dimension: Optional[str]
    ) -> str:
        if "table" in lowered:
            return "table"
        if "bar" in lowered:
            return "bar"
        if "area" in lowered:
            return "area"
        if time_dimension:
            return "line"
        if dimension:
            return "bar"
        return self.settings.default_chart_type


nlq_service = NLQService()

