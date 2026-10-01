from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.analytic_function import AnalyticFunction
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class Statistic(WindowFrameAnalyticFunction):
    """A Postgres statistic over a window - a hare UDF of the same semantics on SQLite."""


class Count(WindowFrameAnalyticFunction):
    def __init__(self, term, **kwargs) -> None:
        super().__init__(AnalyticFunctionName.COUNT, term, **kwargs)


class RowNumber(AnalyticFunction):
    def __init__(self, **kwargs) -> None:
        super().__init__(AnalyticFunctionName.ROW_NUMBER, **kwargs)


class Preceding(WindowFrameAnalyticFunction.Edge):
    modifier = "PRECEDING"


class Following(WindowFrameAnalyticFunction.Edge):
    modifier = "FOLLOWING"
