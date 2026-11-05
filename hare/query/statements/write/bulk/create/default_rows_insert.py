from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.query.execution.write_statements import WriteStatement
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.declarations import BoundInsertBatch
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.sql.builder.returned_value import ReturnedValue

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class DefaultRowsInsert:
    """Rows of objects that leave every column to the database: one statement of default rows where the
    dialect has a source of them, else a DEFAULT VALUES insert per object."""

    @staticmethod
    def bind_batches(
        bulk_create: BulkCreateQuery[Any],
        insert_sql: str,
        objects: list[TModel],
        omit_fields: set[str],
        populate_returned_fields: bool,
    ) -> list[BoundInsertBatch]:
        """Bind all-defaults rows, respecting the requested batch size.

        Each chunk uses a source of default rows where supported, otherwise one
        ``DEFAULT VALUES`` command per object. No statement is sent while binding.

        Args:
            bulk_create: The bulk insert.
            insert_sql: The single-row ``DEFAULT VALUES`` statement.
            objects: The objects to insert.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.
            populate_returned_fields: Set each object's returned columns from its RETURNING row.
        """
        batches: list[BoundInsertBatch] = []
        for chunk in BulkWriteBatches.get_batches(objects, bulk_create._batch_size):
            chunk = list(chunk)
            if not chunk:
                continue
            rows_source = bulk_create._connection.dialect.parameters.get_default_rows_source_sql(len(chunk))
            if rows_source is not None:
                sql = DefaultRowsInsert.build_default_rows_sql(
                    bulk_create, rows_source, omit_fields, populate_returned_fields
                )
                batches.append(
                    BoundInsertBatch(
                        WriteStatement(sql, [], populate_returned_fields), chunk, populate_returned_fields
                    )
                )
            else:
                # The supplied template can include generated columns even when the
                # caller does not ask for them to be assigned to its objects.
                batches.extend(
                    BoundInsertBatch(WriteStatement(insert_sql, [], True), [obj], populate_returned_fields)
                    for obj in chunk
                )
        return batches

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
