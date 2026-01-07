from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Tuple

import pandas as pd
import requests

from backend.api.schemas import CubeQuery
from backend.core.config import settings


class CubeService:
    """Thin Cube client wrapper with a mockable fallback."""

    def __init__(self) -> None:
        self.settings = settings

    def execute(self, cube_query: CubeQuery) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        if not self.settings.cube_api_url:
            return self._mock_data(cube_query), {"source": "mock"}

        headers = {"Content-Type": "application/json"}
        if self.settings.cube_api_token:
            headers["Authorization"] = f"Bearer {self.settings.cube_api_token}"

        try:
            response = requests.post(
                self.settings.cube_api_url, headers=headers, json=cube_query.model_dump()
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data") or payload.get("result", [])
            df = pd.DataFrame(rows)
            if df.empty and self.settings.allow_mock_data:
                return self._mock_data(cube_query), {"source": "mock"}
            return df, {"source": "cube", "raw": payload}
        except Exception as exc:  # noqa: BLE001
            if self.settings.allow_mock_data:
                return self._mock_data(cube_query), {"source": "mock", "error": str(exc)}
            raise

    def _mock_data(self, cube_query: CubeQuery) -> pd.DataFrame:
        """Provide deterministic mock data so the UI stays interactive offline."""
        measure = cube_query.measures[0] if cube_query.measures else "fact_sales.total_sales_amount"
        dimension = cube_query.dimensions[0] if cube_query.dimensions else None
        time_dim = cube_query.timeDimensions[0].dimension if cube_query.timeDimensions else None
        granularity = cube_query.timeDimensions[0].granularity if cube_query.timeDimensions else "day"

        date_range = [
            cube_query.timeDimensions[0].dateRange[0]
            if cube_query.timeDimensions and cube_query.timeDimensions[0].dateRange
            else (datetime.utcnow() - timedelta(days=14)).date().isoformat(),
            cube_query.timeDimensions[0].dateRange[1]
            if cube_query.timeDimensions and cube_query.timeDimensions[0].dateRange
            else datetime.utcnow().date().isoformat(),
        ]

        dates = pd.date_range(start=date_range[0], end=date_range[1], freq="D")
        if granularity and granularity != "day":
            dates = dates.to_period(granularity[0].upper()).to_timestamp()

        records = []
        for idx, ts in enumerate(dates):
            base_value = 1000 + idx * 30
            record: Dict[str, Any] = {measure: base_value}
            if time_dim:
                record[time_dim] = ts.isoformat()
            if dimension:
                record[dimension] = f"Category {idx % 4}"
            records.append(record)

        return pd.DataFrame.from_records(records)


cube_service = CubeService()

