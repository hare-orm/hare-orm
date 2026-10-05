from __future__ import annotations

from typing import Any

from hare.sql.functions.distinct_option_function import DistinctOptionFunction
from hare.sql.terms.star import Star


class Count(DistinctOptionFunction):
    def __init__(self, parameter: Any, alias: str | None = None) -> None:
        is_star = isinstance(parameter, str) and parameter == "*"
        super().__init__("COUNT", Star() if is_star else parameter, alias=alias)
