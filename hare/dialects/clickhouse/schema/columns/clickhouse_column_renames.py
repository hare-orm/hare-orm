from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_renames import ColumnRenames
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
    from hare.models.model_binding import ModelBinding


class ClickhouseColumnRenames(ColumnRenames):
    """ColumnRenames as ClickHouse does it - a column of the table's key (its primary key, sorting,
    partition, sample or sharding key), and one a projection, a materialized view or a dictionary of the
    model reads, can't be renamed: the table is remade with it under its new name, those objects with
    it."""

    __slots__ = ()

    editor: ClickhouseSchemaEditor

    def get_model_column_rename_plan(
        self,
        old_binding: ModelBinding,
        new_binding: ModelBinding,
        field_pairs: list[tuple[Field[Any], Field[Any]]],
    ) -> SchemaChangePlan:
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
            table_rebuild.is_key_column(old_binding, column)
            or table_rebuild.is_read_by_schema_objects(old_binding, field_name, column)
            for field_name, column in renamed_columns
        ):
            return table_rebuild.get_rebuild_plan(old_binding, new_binding, altered_fields=field_pairs)
        return super().get_model_column_rename_plan(old_binding, new_binding, field_pairs)
