from __future__ import annotations

from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction
from hare.sql.terms.functions.window_frame_edge import WindowFrameEdge


class Statistic(WindowFrameAnalyticFunction):
    """A statistic over a window - the database's own function, or hare's where it has none."""

    structure_attributes = ()


class Preceding(WindowFrameEdge):
    structure_attributes = ()

    modifier = "PRECEDING"


class Following(WindowFrameEdge):
    structure_attributes = ()

    modifier = "FOLLOWING"
