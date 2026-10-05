from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.aggregate_function import AggregateFunction


class GroupingFunction(AggregateFunction):
    """``GROUPING(a, b)`` - per group of several groupings, a bit per term set where the group leaves
    the term out (SQL:1999). Computed per group, like an aggregate."""

    def __init__(self, *terms: Any, field_names: tuple[str, ...] = (), alias: str | None = None) -> None:
        super().__init__("GROUPING", *terms, alias=alias)
        #: The names of the grouped fields the terms are of - checked against the query's grouping.
        self.field_names = field_names
