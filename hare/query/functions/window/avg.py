from __future__ import annotations

from typing import ClassVar

from hare.fields.enums import ValueComputation
from hare.query import functions
from hare.query.functions.window.statistic_result_window_function import StatisticResultWindowFunction
from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction


class Avg(StatisticResultWindowFunction):
    """``AVG(field)`` computed over the window."""

    function_name = AnalyticFunctionName.AVG
    analytic_class = WindowFrameAnalyticFunction
    aggregate_class = functions.Avg
    computation: ClassVar[ValueComputation] = ValueComputation.AVERAGE
