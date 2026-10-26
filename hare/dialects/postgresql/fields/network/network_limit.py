from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from hare.fields.narrowing.narrowing_limit import NarrowingLimit

if TYPE_CHECKING:
    from hare.dialects.base.schema.columns.column_narrowing_check import ColumnNarrowingCheck
    from hare.dialects.postgresql.schema.columns.postgresql_column_narrowing_check import (
        PostgresqlColumnNarrowingCheck,
    )


@dataclass(frozen=True)
class NetworkLimit(NarrowingLimit):
    """A value is a network - an address with host bits set beyond its prefix doesn't fit: the conversion
    would drop them."""

    def get_overflow_predicate_sql(self, check: ColumnNarrowingCheck, quoted_column: str) -> str:
        # Only a PostgreSQL field has such a limit - the check is PostgreSQL's.
        return cast("PostgresqlColumnNarrowingCheck", check).get_network_overflow_predicate_sql(quoted_column)
