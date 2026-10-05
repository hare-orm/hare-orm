from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.schema.columns.column_backfill import ColumnBackfill
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_MATERIALIZE_COLUMN_TEMPLATE

if TYPE_CHECKING:  # pragma: nocoverage
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

    async def keep_added_column_default(self, model: type[Model], field: Field[Any], db_field: str) -> None:
        if not field.has_db_default():
            return
        await self.editor.run_sql(
            CLICKHOUSE_MATERIALIZE_COLUMN_TEMPLATE.format(
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
                column=self.editor.quote(db_field),
            )
        )
