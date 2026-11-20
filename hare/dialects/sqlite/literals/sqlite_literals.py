from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from hare.ddl.enums import SchemaNamespace, SchemaPartType
from hare.dialects.base.literals.sql_literals import SqlLiterals
from hare.dialects.sqlite.literals.constants import SQLITE_IDENTIFIER_CASE_FOLDING, SQLITE_MAIN_SCHEMA
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters


class SqliteLiterals(SqlLiterals):
    """SQLite names and literals, using Hare's parameter conversions for dates and decimals."""

    def get_schema_object_key(
        self, object_type: SchemaPartType, schema: str | None, table: str, name: str
    ) -> tuple[SchemaNamespace, str | None, str | None, str] | None:
        """Compare only objects sharing a physical name scope on this database."""
        if object_type is SchemaPartType.TRIGGER:
            return self._get_schema_object_key(SchemaNamespace.TRIGGER, schema, None, name)
        if object_type in (SchemaPartType.TABLE, SchemaPartType.INDEX, SchemaPartType.VIEW):
            return self._get_schema_object_key(SchemaNamespace.RELATION, schema, None, name)
        return None

    def get_literal_writers(self) -> dict[type, Callable[[Any], str]]:
        return {
            **super().get_literal_writers(),
            datetime.datetime: self.get_adapted_moment_literal_sql,
            Decimal: self.get_adapted_decimal_literal_sql,
        }

    def get_schema_key(self, schema: str | None) -> str:
        """Unqualified declarations are created in main, not a distinct namespace."""
        return self.get_identifier_key(SQLITE_MAIN_SCHEMA if schema is None else schema)

    def get_identifier_key(self, name: str) -> str:
        return name.translate(SQLITE_IDENTIFIER_CASE_FOLDING)

    def get_adapted_decimal_literal_sql(self, value: Decimal) -> str:
        """A decimal as the text Hare's parameter converter writes for one - the literal of raw SQL
        holds the same value a bound one does.

        Args:
            value: The decimal.

        Returns:
            The literal.
        """
        return self.get_string_literal_sql(SqliteParameterAdapters.adapt_decimal(value))

    def get_adapted_moment_literal_sql(self, value: datetime.datetime) -> str:
        """A moment as the text Hare's parameter converter writes for one - in UTC when aware,
        with a space separator - so a row filled by the default compares equal to the same
        instant written from Python.

        Args:
            value: The moment.

        Returns:
            The literal.
        """
        return self.get_string_literal_sql(SqliteParameterAdapters.adapt_datetime(value))

    def get_array_literal_sql(self, element_sqls: Sequence[str]) -> str:
        # A JSON array.
        return f"[{','.join(element_sqls)}]"
