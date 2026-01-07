from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio

from backend.api.schemas import CubeQuery, FigureResponse


class VizService:
    """Convert query results into Plotly specs with interaction metadata."""

    def build_figure(
        self, df: pd.DataFrame, cube_query: CubeQuery, chart_type: str, figure_id: str
    ) -> FigureResponse:
        # Build a friendly copy for rendering while preserving member names.
        display_df = df.copy()
        member_map = {col: self._pretty_name(col) for col in display_df.columns}
        display_df.rename(columns=member_map, inplace=True)

        measure = cube_query.measures[0] if cube_query.measures else None
        dimension_candidates: List[str] = []
        if cube_query.timeDimensions:
            dimension_candidates.append(cube_query.timeDimensions[0].dimension)
        dimension_candidates.extend(cube_query.dimensions or [])
        dimension = dimension_candidates[0] if dimension_candidates else None

        if measure:
            measure_label = member_map.get(measure, measure)
        else:
            measure_label = "value"
        dimension_label = member_map.get(dimension, dimension) if dimension else None

        if display_df.empty:
            fig = go.Figure()
            fig.add_annotation(
                text="No data returned yet. Try running a query.",
                showarrow=False,
                x=0.5,
                y=0.5,
                xref="paper",
                yref="paper",
            )
            interaction_mapping: Dict[str, Any] = {}
        elif chart_type == "table":
            fig = go.Figure(
                data=[
                    go.Table(
                        header=dict(values=list(display_df.columns)),
                        cells=dict(values=[display_df[col].tolist() for col in display_df.columns]),
                    )
                ]
            )
            interaction_mapping = {}
        else:
            fig = self._build_chart(
                display_df, chart_type, dimension_label, measure_label, member_map, dimension
            )
            interaction_mapping = {
                "dimension": dimension,
                "dimension_label": dimension_label,
                "measure": measure,
            }

        spec = json.loads(pio.to_json(fig, remove_uids=True))
        meta = {
            "measure": measure,
            "dimension": dimension,
            "chart_type": chart_type,
        }
        return FigureResponse(
            figure_id=figure_id,
            plotly_spec=spec,
            interaction_mapping=interaction_mapping,
            query=cube_query,
            meta=meta,
        )

    def _build_chart(
        self,
        df: pd.DataFrame,
        chart_type: str,
        dimension_label: Optional[str],
        measure_label: str,
        member_map: Dict[str, str],
        dimension_member: Optional[str],
    ) -> go.Figure:
        color = None
        if dimension_label and dimension_label in df.columns and df[dimension_label].nunique() > 12:
            color = dimension_label
        if chart_type == "bar":
            fig = px.bar(df, x=dimension_label, y=measure_label, color=color, template="plotly_white")
        elif chart_type == "area":
            fig = px.area(
                df,
                x=dimension_label,
                y=measure_label,
                color=color,
                template="plotly_white",
            )
        else:
            fig = px.line(df, x=dimension_label, y=measure_label, color=color, template="plotly_white")

        if dimension_label and dimension_label in df.columns:
            fig.update_traces(customdata=df[[dimension_label]])
        fig.update_layout(
            margin=dict(l=20, r=20, t=40, b=20),
            hovermode="x unified" if dimension_member and "date" in dimension_member else "closest",
        )
        fig.update_yaxes(title=measure_label)
        if dimension_label:
            fig.update_xaxes(title=dimension_label)
        return fig

    def _pretty_name(self, member: str) -> str:
        return member.split(".")[-1].replace("_", " ").title()


viz_service = VizService()

