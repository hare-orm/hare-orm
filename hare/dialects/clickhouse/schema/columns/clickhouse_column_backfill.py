from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_backfill import ColumnBackfill
from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.plans.schema_statement import SchemaStatement
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_APPLY_DELETED_MASK_TEMPLATE,
    CLICKHOUSE_MATERIALIZE_COLUMN_TEMPLATE,
    CLICKHOUSE_NOT_NULL_WITH_DEFAULT_TEMPLATE,
    CLICKHOUSE_TYPE_EMPTY_VALUE_SQL,
)

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
    from hare.fields.field import Field
    from hare.models import Model


class ClickhouseColumnBackfill(ColumnBackfill):
    """ColumnBackfill as ClickHouse writes it - a column added to a table isn't written into the
    rows already stored: a row reads its ``DEFAULT`` as the column declares it at the time of
    reading. The added column is materialized, so a later change of its default leaves the stored
    rows the value they got, as on PostgreSQL."""

    __slots__ = ()
    editor: ClickhouseSchemaEditor

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

    def get_not_null_plan(
        self,
        model: type[Model],
        field: Field[Any],
        column: str,
        sql_type: str,
        *,
        column_has_db_default: bool,
    ) -> SchemaChangePlan:
        """The ALTER making a column NOT NULL - ClickHouse 25.12+ refuses it to a column with no DEFAULT
        for its NULL rows: the field's ``db_default`` is given with the change, else the type's empty
        value, dropped again by a second ALTER (one can't drop a DEFAULT another change of it adds).

        The rows a lightweight delete hides are dropped first: the change converts them too, and the NULL
        backfill before it reads no deleted row - a NULL left in one would fail the change halfway.
        """
        editor = self.editor
        apply_deleted_mask_sql = CLICKHOUSE_APPLY_DELETED_MASK_TEMPLATE.format(
            table=editor.get_row_change_table_sql(model)
        )
        mask_step = SchemaStatement(
            apply_deleted_mask_sql,
            frozenset({SchemaEffect.REWRITE_ROWS}),
            recovery=editor.get_row_recovery(model),
        )
        if column_has_db_default:
            plan = super().get_not_null_plan(
                model, field, column, sql_type, column_has_db_default=column_has_db_default
            )
            return SchemaChangePlan((mask_step, *plan.steps))
        if field.has_db_default():
            default_sql = editor.column_definitions.get_field_db_default_sql(field, model)
        else:
            type_name = editor.client.dialect.literals.get_string_literal_sql(sql_type)
            default_sql = CLICKHOUSE_TYPE_EMPTY_VALUE_SQL.format(type_name=type_name)
        statements = [
            editor.get_column_change_sql(
                model,
                editor.ALTER_FIELD_TEMPLATE,
                changes=CLICKHOUSE_NOT_NULL_WITH_DEFAULT_TEMPLATE.format(
                    column=column, sql_type=sql_type, default=default_sql
                ),
            ),
        ]
        if not field.has_db_default():
            statements.append(
                editor.get_column_change_sql(
                    model,
                    editor.ALTER_FIELD_TEMPLATE,
                    changes=editor.ALTER_FIELD_DROP_DEFAULT_TEMPLATE.format(column=column),
                )
            )
        recovery = editor.get_schema_recovery(model)
        return SchemaChangePlan(
            (
                mask_step,
                *(SchemaStatement(sql, recovery=recovery) for sql in statements),
            )
        )

    def get_added_column_default_plan(self, model: type[Model], field: Field[Any], db_field: str) -> SchemaChangePlan:
        """Store the added column's default so later changes cannot alter existing rows."""
        if not field.has_db_default():
            return SchemaChangePlan(())
        return SchemaChangePlan(
            (
                SchemaStatement(
                    CLICKHOUSE_MATERIALIZE_COLUMN_TEMPLATE.format(
                        table=self.editor.get_row_change_table_sql(model),
                        column=self.editor.quote(db_field),
                    ),
                    frozenset({SchemaEffect.BACKFILL_ROWS}),
                    recovery=self.editor.get_row_recovery(model),
                ),
            )
        )
