from __future__ import annotations

from typing import Any

from hare.sql.functions.cast import Cast
from hare.sql.functions.distinct_option_function import DistinctOptionFunction


class StringAggFunction(DistinctOptionFunction):
    def __init__(self, term: Any, delimiter: Any, alias: str | None = None) -> None:
        super().__init__("STRING_AGG", Cast(term, "TEXT"), delimiter, alias=alias)
