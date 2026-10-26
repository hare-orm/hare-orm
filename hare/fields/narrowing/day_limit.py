from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck


@dataclass(frozen=True)
class DayLimit(NarrowingLimit):
    """A value is a day from ``first_day`` to ``last_day`` - None for no bound - and, where the old column
    held a time of day, a moment at midnight."""

    first_day: datetime.date | None
    last_day: datetime.date | None
    checks_time_of_day: bool

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        return check.get_day_overflow_predicate_sql(self, quoted_column)
