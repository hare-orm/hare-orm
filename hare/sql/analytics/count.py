from __future__ import annotations

from typing import Any

from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class Count(WindowFrameAnalyticFunction):
    """``COUNT(term) OVER (...)`` - the window's count of the values that aren't NULL."""

    def __init__(self, term: Any, **kwargs: Any) -> None:
        super().__init__(AnalyticFunctionName.COUNT, term, **kwargs)
