from __future__ import annotations

from dataclasses import dataclass, fields

from hare.sql.sql_context import SqlContext


@dataclass(frozen=True)
class ClickhouseSqlContext(SqlContext):
    """The context ClickHouse's SQL renders in.

    Attributes:
        rows_are_grouped: The statement being rendered has a ``GROUP BY`` - each of its aggregates
            reads at least one row.
    """

    rows_are_grouped: bool = False

    @classmethod
    def from_context(cls, sql_context: SqlContext) -> ClickhouseSqlContext:
        """The context holding what another one holds.

        Args:
            sql_context: The context.

        Returns:
            The ClickHouse context.
        """
        return cls(**{field.name: getattr(sql_context, field.name) for field in fields(SqlContext)})
