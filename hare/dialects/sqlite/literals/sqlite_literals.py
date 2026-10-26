from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from hare.dialects.base.literals.sql_literals import SqlLiterals
from hare.dialects.sqlite.literals.constants import SQLITE_IDENTIFIER_CASE_FOLDING
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters


class SqliteLiterals(SqlLiterals):
    """SQLite's names and literals - datetimes as the sqlite3 adapter writes them, arrays as JSON."""

    def get_literal_writers(self) -> dict[type, Callable[[Any], str]]:
        return {
            **super().get_literal_writers(),
            datetime.datetime: self.get_adapted_moment_literal_sql,
            Decimal: self.get_adapted_decimal_literal_sql,
        }

    def get_identifier_key(self, name: str) -> str:
        return name.translate(SQLITE_IDENTIFIER_CASE_FOLDING)

    def get_adapted_decimal_literal_sql(self, value: Decimal) -> str:
        """A decimal as the text the sqlite3 parameter adapter writes for one - the literal of raw SQL
        holds the same value a bound one does.

        Args:
            value: The decimal.

        Returns:
            The literal.
        """
        return self.get_string_literal_sql(SqliteParameterAdapters.adapt_decimal(value))

    def get_adapted_moment_literal_sql(self, value: datetime.datetime) -> str:
        """A moment as the text the sqlite3 parameter adapter writes for one - in UTC when aware,
        with a space separator - so a row filled by the default compares equal to the same
        instant written from Python.

        Args:
            value: The moment.

        Returns:
            The literal.
        """
        if value.tzinfo is not None:
            value = value.astimezone(datetime.UTC)
        return self.get_string_literal_sql(value.isoformat(" "))

    def get_array_literal_sql(self, element_sqls: Sequence[str]) -> str:
        # A JSON array.
        return f"[{','.join(element_sqls)}]"
