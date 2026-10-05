from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_definitions import ColumnDefinitions
from hare.dialects.clickhouse.types.clickhouse_nullable_types import ClickhouseNullableTypes

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.declarations import ClickhouseSchemaEditor
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseColumnDefinitions(ColumnDefinitions):
    """The columns of a ClickHouse table - a column holds no NULL unless its type is
    ``Nullable(...)``, which ``ALTER TABLE ... ADD COLUMN`` takes as ``CREATE TABLE`` does. The
    table's primary key is the ``PRIMARY KEY`` clause of its engine (``ClickhouseTableOptions``),
    never a column's. A column the database computes is ``type MATERIALIZED expression`` or ``type
    ALIAS expression`` - its ``Nullable`` around the type alone."""

    __slots__ = ()

    editor: ClickhouseSchemaEditor

    def get_altered_column_type(self, sql_type: str, nullable: bool) -> str:
        return ClickhouseNullableTypes.get_nullable_type(sql_type) if nullable else sql_type

    def get_field_sql(
        self,
        db_field: str,
        field_type: str,
        nullable: bool,
        primary_key_name: str | None,
        comment: str,
        default: str = "",
    ) -> str:
        return self.editor.FIELD_TEMPLATE.format(
            name=self.editor.quote(db_field),
            type=self.get_altered_column_type(field_type, nullable),
            nullable="",
            primary="",
            default=default,
            comment=comment,
        ).strip()

    def get_non_pk_generated_field_sql(
        self, model: type[Model], field_object: Field[Any], db_field: str, comment: str = ""
    ) -> str | None:
        if not field_object.generated or field_object.pk:
            return None
        generated_sql = self.get_generated_column_sql(field_object)
        if not generated_sql:
            return None
        column_type = self.get_altered_column_type(self.get_table_column_type(model, field_object), field_object.null)
        return self.editor.FIELD_TEMPLATE.format(
            name=self.editor.quote(db_field),
            type=f"{column_type} {generated_sql}",
            nullable="",
            primary="",
            default="",
            comment=comment,
        ).strip()
