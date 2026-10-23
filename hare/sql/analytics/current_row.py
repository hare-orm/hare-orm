from __future__ import annotations

from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class CurrentRow(WindowFrameAnalyticFunction.Edge):
    """The current row as an edge of a window frame."""

    def __str__(self) -> str:
        return "CURRENT ROW"
