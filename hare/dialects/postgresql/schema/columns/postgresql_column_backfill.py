from __future__ import annotations

from hare.dialects.base.schema.columns.column_backfill import ColumnBackfill
from hare.dialects.postgresql.schema.constants import POSTGRESQL_ROW_IDENTITY_COLUMN


class PostgresqlColumnBackfill(ColumnBackfill):
    """ColumnBackfill as PostgreSQL writes it."""

    __slots__ = ()

    def get_row_identity_sql(self) -> str | None:
        return POSTGRESQL_ROW_IDENTITY_COLUMN
