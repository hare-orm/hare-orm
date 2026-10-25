from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.schema_objects.schemas import Schemas
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import CLICKHOUSE_CURRENT_DATABASE_SQL
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
    from hare.models import Model


class ClickhouseSchemas(Schemas):
    """Schemas as ClickHouse has them - a model's schema is a database of the server: a table moves to
    another one with ``RENAME TABLE``, which keeps its rows."""

    __slots__ = ()

    editor: ClickhouseSchemaEditor

    def raise_if_model_unmovable(self, model: type[Model]) -> None:
        """Refuses moving a model whose views, materialized views or dictionaries name its table by its
        database, or whose table is a ``Distributed`` one naming its local table - ``RENAME TABLE``
        moves the table alone.

        Args:
            model: The model, rendered in the database it moves from.

        Raises:
            UnSupportedError: The model declares such an object, or its table is distributed.
        """
        meta = model._meta
        if meta.views or meta.materialized_views or meta.dictionaries:
            raise UnSupportedError(
                f"{model.__name__} declares views, materialized views or dictionaries, which name its table "
                "by its database on ClickHouse - remove them before moving the table to another database, "
                "and add them back after"
            )
        table_options = meta.get_table_options(self.editor.client.dialect)
        if isinstance(table_options, ClickhouseTableOptions) and table_options.distributed_over is not None:
            raise UnSupportedError(
                f"{model.__name__}'s table is a Distributed table over {table_options.distributed_over!r}, "
                "which names its local table by its database - a distributed table isn't moved to another database"
            )

    async def move_table_to_schema(self, table_name: str, old_schema: str | None, new_schema: str | None) -> None:
        """Moves a table to another database of the server with ``RENAME TABLE``, which keeps its rows.

        Args:
            table_name: The table.
            old_schema: The database it is in; None for the connection's database.
            new_schema: The database it moves to; None for the connection's database.
        """
        if (old_schema or None) == (new_schema or None):
            return
        if old_schema is None or new_schema is None:
            # The connection's database named by its name - the table stays where it is.
            rows = await self.editor.client.execute_dicts(CLICKHOUSE_CURRENT_DATABASE_SQL)
            database = str(rows[0]["name"])
            if (old_schema or database) == (new_schema or database):
                return
        await self.editor.run_sql(
            self.editor.RENAME_TABLE_TEMPLATE.format(
                old_table=self.editor.qualify_table_name(table_name, old_schema),
                new_table=self.editor.qualify_table_name(table_name, new_schema),
            )
        )
