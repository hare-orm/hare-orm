from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.clickhouse.constants import CLICKHOUSE_CURRENT_DATABASE_SQL
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_SCHEMA_MOVE_PREVIEW

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class ClickhouseTableSchemaMove:
    """A prepared rename whose source or target is the connection's current database.

    The current database is read only during execution. If both names identify it,
    the table stays where it is; otherwise execution uses the already rendered SQL.
    Preview describes that condition without reading the server.
    """

    sql: str
    old_schema: str | None
    new_schema: str | None
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.READ_CATALOG})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Compare the actual database names before running the prepared rename."""
        if editor.collect_sql:
            editor.collected_sql.extend((CLICKHOUSE_SCHEMA_MOVE_PREVIEW, self.sql))
            return
        rows = await editor.client.execute_dicts(CLICKHOUSE_CURRENT_DATABASE_SQL)
        database = str(rows[0]["name"])
        if (self.old_schema or database) != (self.new_schema or database):
            await editor.run_sql(self.sql)
