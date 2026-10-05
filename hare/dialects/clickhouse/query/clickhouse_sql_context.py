from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from hare.dialects.clickhouse.constants import CLICKHOUSE_BACKSLASH, CLICKHOUSE_ESCAPED_BACKSLASH
from hare.sql.sql_context import SqlContext


@dataclass(frozen=True)
class ClickhouseSqlContext(SqlContext):
    """The context ClickHouse's SQL renders in.

    Attributes:
        rows_are_grouped: The statement being rendered has a ``GROUP BY`` - each of its aggregates
            reads at least one row.
        reads_distributed_table: The statement being rendered reads a ``Distributed`` table - it
            runs on every shard, so a table it joins and a subquery of its ``IN`` are read once,
            ``GLOBAL``, and sent to them.
    """

    rows_are_grouped: bool = False
    reads_distributed_table: bool = False

    def quote(self, name: Any) -> str:
        # A backslash escapes the next character of a quoted name - doubled, it is itself.
        return SqlContext.quote_text(
            str(name).replace(CLICKHOUSE_BACKSLASH, CLICKHOUSE_ESCAPED_BACKSLASH), self.quote_char
        )

    def quote_alias(self, alias: Any) -> str:
        return SqlContext.quote_text(
            str(alias).replace(CLICKHOUSE_BACKSLASH, CLICKHOUSE_ESCAPED_BACKSLASH),
            self.alias_quote_char or self.quote_char,
        )

    @classmethod
    def from_context(cls, sql_context: SqlContext) -> ClickhouseSqlContext:
        """The context holding what another one holds.

        Args:
            sql_context: The context.

        Returns:
            The ClickHouse context.
        """
        return cls(**{field.name: getattr(sql_context, field.name) for field in fields(SqlContext)})
