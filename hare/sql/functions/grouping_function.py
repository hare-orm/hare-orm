from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.aggregate_function import AggregateFunction


class GroupingFunction(AggregateFunction):
    """``GROUPING(a, b)`` - per group of several groupings, a bit per term set where the group leaves
    the term out (SQL:1999). Computed per group, like an aggregate."""

    def __init__(self, *terms: Any, alias: str | None = None) -> None:
        super().__init__("GROUPING", *terms, alias=alias)
