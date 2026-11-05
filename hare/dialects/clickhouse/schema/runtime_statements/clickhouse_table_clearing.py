from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.schema.runtime_statements.table_clearing import TableClearing

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models import Model


class ClickhouseTableClearing(TableClearing):
    """Tables emptied with ``TRUNCATE TABLE`` - a ClickHouse DELETE needs a condition and marks
    every row deleted one by one."""

    __slots__ = ()

    @staticmethod
    def get_table_target_sql(editor: BaseSchemaEditor, table_name: str, schema: str | None) -> str:
        return editor.get_schema_table_sql(table_name, schema)

    @staticmethod
    def get_model_target_sql(editor: BaseSchemaEditor, model: type[Model]) -> str:
        return editor.get_storage_table_sql(model)

    @staticmethod
    async def clear_tables(connection: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        if quoted_tables:
            script = "".join(f"TRUNCATE TABLE {quoted_table};\n" for quoted_table in quoted_tables)  # nosec
            await connection.execute_built_script(script)
