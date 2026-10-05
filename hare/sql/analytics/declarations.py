from __future__ import annotations

from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class Statistic(WindowFrameAnalyticFunction):
    """A Postgres statistic over a window - a hare UDF of the same semantics on SQLite."""


class Preceding(WindowFrameAnalyticFunction.Edge):
    modifier = "PRECEDING"


class Following(WindowFrameAnalyticFunction.Edge):
    modifier = "FOLLOWING"
