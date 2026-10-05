from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.dialects.base.schema.indexes.index_statements import IndexStatements

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlIndexStatements(IndexStatements):
    """IndexStatements as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def format_index_type(self, index_type: str) -> str:
        return f"USING {index_type} "

    @classmethod
    def get_index_include_sql(cls, quoted_columns: Sequence[str]) -> str:
        return f" INCLUDE ({', '.join(quoted_columns)})"
