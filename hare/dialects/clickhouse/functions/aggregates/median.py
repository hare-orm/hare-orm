from __future__ import annotations

from typing import Any

from hare.dialects.clickhouse.functions.aggregates.quantile import Quantile


class Median(Quantile):
    """``quantile(0.5)(field)`` - the median of a group's values, an estimate; with ``exact=True`` the
    middle value itself.

    Args:
        field: The field or expression.
        exact: Whether the median is found over every value, not estimated.
    """

    def __init__(self, field: Any, *, exact: bool = False, **kwargs: Any) -> None:
        super().__init__(field, 0.5, exact=exact, **kwargs)
