from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
    from hare.dialects.clickhouse.schema.columns.clickhouse_column_narrowing_check import (
        ClickhouseColumnNarrowingCheck,
    )


@dataclass(frozen=True)
class MomentLimit(NarrowingLimit):
    """A value is a moment from ``first_moment`` to ``last_moment`` with no more than ``digits`` digits
    after the second."""

    first_moment: datetime.datetime
    last_moment: datetime.datetime
    digits: int

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        # Only a ClickHouse field has such a limit - the check is ClickHouse's.
        return cast("ClickhouseColumnNarrowingCheck", check).get_moment_overflow_predicate_sql(self, quoted_column)
