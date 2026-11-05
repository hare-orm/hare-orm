from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.bound_index_key import BoundIndexKey
from hare.dialects.base.schema.indexes.index_statements import IndexStatements
from hare.dialects.clickhouse.constants import CLICKHOUSE_DEFAULT_INDEX_GRANULARITY_SQL, CLICKHOUSE_DEFAULT_INDEX_TYPE

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
    from hare.models import Model


class ClickhouseIndexStatements(IndexStatements):
    """ClickHouse's ``CREATE INDEX`` - a data skipping index, its type and granularity written after
    its keys; an index of no type is a ``minmax`` one of granularity 1."""

    editor: ClickhouseSchemaEditor

    def get_index_sql(
        self,
        model: type[Model],
        field_names: Sequence[str | BoundIndexKey],
        safe: bool = False,
        index_name: str | None = None,
        index_type: str | None = None,
        extra: str | None = None,
        opclasses: Sequence[str] | None = None,
        unique: bool = False,
        orders: Sequence[str] | None = None,
        indexed_table_sql: str | None = None,
        template: str | None = None,
    ) -> str:
        return super().get_index_sql(
            model,
            field_names,
            safe,
            index_name=index_name,
            index_type=index_type or CLICKHOUSE_DEFAULT_INDEX_TYPE,
            extra=extra or CLICKHOUSE_DEFAULT_INDEX_GRANULARITY_SQL,
            opclasses=opclasses,
            unique=unique,
            orders=orders,
            indexed_table_sql=indexed_table_sql or self.editor.get_storage_table_sql(model),
            template=template,
        )

    def format_index_type(self, index_type: str) -> str:
        return f" TYPE {index_type}"

    def get_table_index_sql(
        self, table_name: str, column_names: Sequence[str], schema: str | None = None, safe: bool = False
    ) -> str:
        return self.editor.INDEX_CREATE_TEMPLATE.format(
            exists=self.editor.table_creation.get_exists_sql(safe),
            index_name=self.editor.quote(
                GeneratedNames.get_index_name(GeneratedNamePrefix.INDEX, table_name, column_names)
            ),
            index_type=self.format_index_type(CLICKHOUSE_DEFAULT_INDEX_TYPE),
            table_name=self.editor.get_schema_table_sql(table_name, schema),
            fields=", ".join([self.editor.quote(column_name) for column_name in column_names]),
            extra=CLICKHOUSE_DEFAULT_INDEX_GRANULARITY_SQL,
        )

    def get_table_index_drop_sql(self, table_name: str, index_name: str, schema: str | None = None) -> str:
        """Drop a table-scoped index from declared storage, without qualifying its local name."""
        return self.editor.DROP_INDEX_TEMPLATE.format(
            table=self.editor.get_schema_table_sql(table_name, schema),
            name=self.editor.quote(index_name),
        )
