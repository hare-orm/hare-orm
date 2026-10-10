from __future__ import annotations

from typing import Any

from hare.sql.enums import AnalyticFunctionName
from hare.sql.terms.functions.analytic_function import AnalyticFunction


class RowNumber(AnalyticFunction):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(AnalyticFunctionName.ROW_NUMBER, **kwargs)
