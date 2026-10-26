from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from hare.fields.enums import NarrowedValueSource
from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
    from hare.dialects.clickhouse.schema.columns.clickhouse_column_narrowing_check import (
        ClickhouseColumnNarrowingCheck,
    )


@dataclass(frozen=True)
class ByteLengthLimit(NarrowingLimit):
    """A value's text takes no more than ``max_length`` bytes of its UTF-8 - a ``FixedString`` holds bytes."""

    max_length: int
    source: NarrowedValueSource

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        # Only a ClickHouse field has such a limit - the check is ClickHouse's.
        return cast("ClickhouseColumnNarrowingCheck", check).get_byte_length_overflow_predicate_sql(
            self, quoted_column
        )
