"""
Package for SQL analytic functions wrappers
"""

from __future__ import annotations

from hare.sql.analytics.count import Count
from hare.sql.analytics.declarations import Following, Preceding, Statistic
from hare.sql.analytics.row_number import RowNumber

__all__ = [
    "Preceding",
    "Following",
    "RowNumber",
    "Statistic",
    "Count",
]
