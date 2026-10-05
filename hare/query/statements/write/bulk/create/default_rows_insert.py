from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.sql.builder.returned_value import ReturnedValue

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class DefaultRowsInsert:
    """Rows of objects that leave every column to the database: one statement of default rows where the
    dialect has a source of them, else a DEFAULT VALUES insert per object."""

    @staticmethod
    async def insert_default_rows(
        bulk_create: BulkCreateQuery[Any],
        insert_sql: str,
        objects: list[TModel],
        omit_fields: set[str],
        populate_returned_fields: bool,
    ) -> None:
        """Inserts one all-defaults row per object - a single ``INSERT ... SELECT FROM
        generate_series(...)`` on Postgres, one ``DEFAULT VALUES`` statement per object on SQLite,
        which has no multi-row form of it.

        Args:
            bulk_create: The bulk insert.
            insert_sql: The single-row ``DEFAULT VALUES`` statement.
            objects: The objects to insert.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.
            populate_returned_fields: Set each object's returned columns from its RETURNING row.
        """
        if not objects:
            return
        if (
            default_rows_source_sql := bulk_create._connection.dialect.parameters.get_default_rows_source_sql(
                len(objects)
            )
        ) is not None:
            _, returned_rows = await bulk_create._connection.execute(
                DefaultRowsInsert.build_default_rows_sql(
                    bulk_create, default_rows_source_sql, omit_fields, populate_returned_fields
                ),
                [],
            )
            if populate_returned_fields:
                ReturnedRowsMatching.populate_returned_fields_from_returning_rows(bulk_create, objects, returned_rows)
            return
        for obj in objects:
            # insert_sql (self._writer.insert_query) already carries its own RETURNING clause
            # for every generated_db_fields column (pk included, when auto-generated).
            returned_rows = (await bulk_create._connection.execute(insert_sql, [], returns_rows=True)).rows
            if populate_returned_fields and returned_rows:
                ReturnedRowsMatching.populate_returned_fields_from_returning_rows(
                    bulk_create, [obj], [returned_rows[0]]
                )

    @staticmethod
    def build_default_rows_sql(
        bulk_create: BulkCreateQuery[Any], rows_source_sql: str, omit_fields: set[str], with_returning: bool
    ) -> str:
        """``INSERT INTO t <rows_source_sql>`` - rows of column defaults in one statement.

        Args:
            bulk_create: The bulk insert.
            rows_source_sql: The dialect's source of the rows.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.
            with_returning: Add a RETURNING clause for the pk, generated and omitted columns.

        Returns:
            The SQL text.
        """
        sql_context = bulk_create._connection.query_class.SQL_CONTEXT
        returning_columns = (
            ReturnedRowsMatching.get_returning_columns(bulk_create, omit_fields) if with_returning else []
        )
        return bulk_create._connection.dialect.clauses.get_insert_rows_source_sql(
            bulk_create.model._meta.basetable.get_sql(sql_context),
            rows_source_sql,
            [ReturnedValue(sql=sql_context.quote(column), column_name=column) for column in returning_columns],
        )
