from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_backfill import ColumnBackfill
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_APPLY_DELETED_MASK_TEMPLATE,
    CLICKHOUSE_MATERIALIZE_COLUMN_TEMPLATE,
    CLICKHOUSE_NOT_NULL_WITH_DEFAULT_TEMPLATE,
    CLICKHOUSE_TYPE_EMPTY_VALUE_SQL,
)

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseColumnBackfill(ColumnBackfill):
    """ColumnBackfill as ClickHouse writes it - a column added to a table isn't written into the
    rows already stored: a row reads its ``DEFAULT`` as the column declares it at the time of
    reading. The added column is materialized, so a later change of its default leaves the stored
    rows the value they got, as on PostgreSQL."""

    __slots__ = ()

    def get_backfill_batch_sql(
        self, qualified_table: str, quoted_column: str, quoted_key_columns: list[str], batch_size: int
    ) -> str:
        """The one mutation filling a column's NULLs in every row: a mutation locks no row a batch
        would spare, each one rewrites the parts it changes, and it reports no count of rows for the
        batches to end by.

        Args:
            qualified_table: The qualified, quoted table.
            quoted_column: The quoted column to fill.
            quoted_key_columns: Not used.
            batch_size: Not used.

        Returns:
            The statement.
        """
        dialect = self.editor.client.dialect
        return self.editor.UPDATE_ROWS_TEMPLATE.format(
            table=qualified_table,
            assignments=f"{quoted_column} = {dialect.parameters.get_placeholder(1)}",
            condition=(
                f"{quoted_column} IS NULL "
                f"AND {dialect.renderers.get_distinct_from_sql(quoted_column, dialect.parameters.get_placeholder(2))}"
            ),
        )

    def get_not_null_sql(
        self,
        model: type[Model],
        field: Field[Any],
        qualified_table: str,
        column: str,
        sql_type: str,
        *,
        column_has_db_default: bool,
    ) -> str:
        """The ALTER making a column NOT NULL - ClickHouse 25.12+ refuses it to a column with no DEFAULT
        for its NULL rows: the field's ``db_default`` is given with the change, else the type's empty
        value, dropped again by a second ALTER (one can't drop a DEFAULT another change of it adds).

        The rows a lightweight delete hides are dropped first: the change converts them too, and the NULL
        backfill before it reads no deleted row - a NULL left in one would fail the change halfway.
        """
        editor = self.editor
        apply_deleted_mask_sql = CLICKHOUSE_APPLY_DELETED_MASK_TEMPLATE.format(table=qualified_table)
        if column_has_db_default:
            not_null_sql = super().get_not_null_sql(
                model, field, qualified_table, column, sql_type, column_has_db_default=column_has_db_default
            )
            return ";\n".join((apply_deleted_mask_sql, not_null_sql))
        if field.has_db_default():
            default_sql = editor.column_definitions.get_field_db_default_sql(field, model)
        else:
            type_name = editor.client.dialect.literals.get_string_literal_sql(sql_type)
            default_sql = CLICKHOUSE_TYPE_EMPTY_VALUE_SQL.format(type_name=type_name)
        statements = [
            apply_deleted_mask_sql,
            editor.ALTER_FIELD_TEMPLATE.format(
                table=qualified_table,
                changes=CLICKHOUSE_NOT_NULL_WITH_DEFAULT_TEMPLATE.format(
                    column=column, sql_type=sql_type, default=default_sql
                ),
            ),
        ]
        if not field.has_db_default():
            statements.append(
                editor.ALTER_FIELD_TEMPLATE.format(
                    table=qualified_table, changes=editor.ALTER_FIELD_DROP_DEFAULT_TEMPLATE.format(column=column)
                )
            )
        return ";\n".join(statements)

    async def keep_added_column_default(self, model: type[Model], field: Field[Any], db_field: str) -> None:
        if not field.has_db_default():
            return
        await self.editor.run_sql(
            CLICKHOUSE_MATERIALIZE_COLUMN_TEMPLATE.format(
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                column=self.editor.quote(db_field),
            )
        )
