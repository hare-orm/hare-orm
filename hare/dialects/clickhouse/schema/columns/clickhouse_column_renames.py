from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_renames import ColumnRenames
from hare.fields.field import Field
from hare.models import Model

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.declarations import ClickhouseSchemaEditor


class ClickhouseColumnRenames(ColumnRenames):
    """ColumnRenames as ClickHouse does it - a column of the table's key (its primary key, sorting,
    partition, sample or sharding key), and one a projection, a materialized view or a dictionary of the
    model reads, can't be renamed: the table is remade with it under its new name, those objects with
    it."""

    __slots__ = ()

    editor: ClickhouseSchemaEditor

    async def rename_model_columns(
        self,
        old_model: type[Model],
        new_model: type[Model],
        field_pairs: list[tuple[Field[Any], Field[Any]]],
    ) -> bool:
        renamed_columns = [
            (old_field.model_field_name, old_column)
            for old_field, new_field in field_pairs
            for old_column, new_column in zip(
                self.get_column_names(old_field), self.get_column_names(new_field), strict=True
            )
            if old_column != new_column
        ]
        table_rebuild = self.editor.table_rebuild
        if any(
            table_rebuild.is_key_column(old_model, column)
            or table_rebuild.is_read_by_schema_objects(old_model, field_name, column)
            for field_name, column in renamed_columns
        ):
            await self.editor.table_rebuild.remake_table(old_model, new_model, altered_fields=field_pairs)
            return True
        return await super().rename_model_columns(old_model, new_model, field_pairs)
