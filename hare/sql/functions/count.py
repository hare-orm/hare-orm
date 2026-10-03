from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.terms.star import Star

if TYPE_CHECKING:
    pass
from hare.sql.functions.distinct_option_function import DistinctOptionFunction


class Count(DistinctOptionFunction):
    def __init__(self, param: Any, alias: str | None = None) -> None:
        is_star = isinstance(param, str) and param == "*"
        super().__init__("COUNT", Star() if is_star else param, alias=alias)
