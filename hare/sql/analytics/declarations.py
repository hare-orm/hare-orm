from __future__ import annotations

from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class Statistic(WindowFrameAnalyticFunction):
    """A statistic over a window - the database's own function, or hare's where it has none."""


class Preceding(WindowFrameAnalyticFunction.Edge):
    modifier = "PRECEDING"


class Following(WindowFrameAnalyticFunction.Edge):
    modifier = "FOLLOWING"
