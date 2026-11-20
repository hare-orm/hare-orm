from __future__ import annotations

from hare.sql.builder.tables.table import Table
from hare.sql.sql_context import SqlContext


class TriggerRow(Table):
    """The NEW or OLD row supplied by a trigger, with an unquoted SQL keyword namespace."""

    def __init__(self, row: str) -> None:
        super().__init__(row)

    def get_column_sql(self, name: str, sql_context: SqlContext) -> str:
        return f"{self.get_name_without_alias()}.{sql_context.quote(name)}"
