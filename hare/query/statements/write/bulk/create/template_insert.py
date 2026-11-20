from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from hare.query.execution.write_statements import WriteMany
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.query.statements.write.bulk.create.declarations import BoundInsertBatch
from hare.query.statements.write.bulk.create.inline_insert import InlineInsert
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.terms.parameters.parameterizer import Parameterizer

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.core.caching.cache_bucket import CacheBucket
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class TemplateInsert:
    """The single-row INSERT template of a bulk insert, with the values of its ON CONFLICT clause as
    parameters after the row's own, run once per object through execute_many() - and the DEFAULT
    VALUES insert of one row, which is that template without columns."""

    @staticmethod
    def get_row_template(
        bulk_create: BulkCreateQuery[Any], columns: list[str] | None, returning: Sequence[str] = ()
    ) -> tuple[str, list[Any]]:
        """The single-row INSERT template of this call's ``ON CONFLICT`` clause, kept in
        ``StatementPlans.insert_templates``.

        Args:
            bulk_create: The bulk insert.
            columns: The row's columns; None for the ``DEFAULT VALUES`` INSERT.
            returning: The columns the INSERT returns.

        Returns:
            The SQL text and the values of the parameters following the row's.
        """
        conflict = None if columns is None else ConflictClause.get_conflict(bulk_create)
        meta: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        key = (
            bulk_create.model,
            bulk_create._connection.connection_alias,
            bulk_create._connection.dialect,
            meta.query_class,
            meta.schema,
            meta.db_table,
            None if columns is None else tuple(columns),
            conflict,
            tuple(returning),
        )
        bucket: CacheBucket[tuple[str, list[Any]]] | None
        try:
            bucket = StatementPlans.insert_templates.get_bucket(bulk_create._binding)
            template = bucket.get(key)
        except TypeError:
            # A tenant value the conflict update is limited to that can't be hashed - built each time.
            bucket = None
            template = None
        if template is not None:
            return template
        insert_statement = bulk_create._get_insert_statement()
        if columns is None:
            template = (str(insert_statement.get_query([], [], returning=returning)), [])
        else:
            query = insert_statement.get_query(
                columns, [insert_statement.get_parameter_row(len(columns))], conflict=conflict, returning=returning
            )
            template = TemplateInsert.get_row_template_sql(query, len(columns))
        if bucket is not None:
            bucket[key] = template
        return template

    @staticmethod
    def get_row_template_sql(query: QueryBuilder, row_parameter_count: int) -> tuple[str, list[Any]]:
        """Renders a single-row INSERT template, binding the values of its ``ON CONFLICT`` clause as
        parameters numbered after the row's own placeholders.

        Args:
            query: The INSERT query, its row values already placeholders.
            row_parameter_count: How many placeholders the row takes.

        Returns:
            The SQL text and the values of the parameters following the row's.
        """
        parameterizer = Parameterizer()
        parameterizer.values.extend([None] * row_parameter_count)
        sql = query.get_sql(query.query_class.SQL_CONTEXT.copy(parameterizer=parameterizer))
        return sql, parameterizer.values[row_parameter_count:]

    @staticmethod
    def bind_batches(
        bulk_create: BulkCreateQuery[Any],
        db_columns: list[str],
        field_names: list[str],
        objects: list[TModel],
        *,
        parameters_inline: bool = False,
    ) -> list[BoundInsertBatch]:
        """Binds the objects to one statement template with one parameter row per object (``execute_many()``),
        batched by ``batch_size``.

        Args:
            bulk_create: The insert.
            db_columns: The columns written.
            field_names: Their fields.
            objects: The objects.
            parameters_inline: Retain literal SQL for each row of a preview.
        """
        sql, statement_values = TemplateInsert.get_row_template(bulk_create, db_columns)
        batches: list[BoundInsertBatch] = []
        model_binding: ModelBinding = bulk_create._binding  # type: ignore[assignment]
        sensitive_positions = BulkWriteBatches.get_sensitive_positions(model_binding, field_names)
        for objects_item in BulkWriteBatches.get_batches(objects, bulk_create._batch_size):
            objects_item = list(objects_item)
            if not objects_item:
                continue
            rows = BulkWriteBatches.serialize_instances(
                model_binding, bulk_create._connection.dialect.types, objects_item, field_names
            )
            inline_statements = (
                tuple(InlineInsert.render(bulk_create, db_columns, [row]) for row in rows) if parameters_inline else ()
            )
            if statement_values:
                rows = [[*row, *statement_values] for row in rows]
            if sensitive_positions:
                rows = BulkWriteBatches.hide_row_values(rows, sensitive_positions)
            batches.append(BoundInsertBatch(WriteMany(sql, rows), objects_item, inline_statements=inline_statements))
        return batches

    @staticmethod
    def get_default_values_sql(
        bulk_create: BulkCreateQuery[Any], omit_fields: set[str], *, primary_key_given: bool
    ) -> str:
        """The ``DEFAULT VALUES`` INSERT of one row - every column left to the database - returning
        its columns when ``returning=True``.

        Args:
            bulk_create: The bulk insert.
            omit_fields: Fields left out because every object relies on its database default.
            primary_key_given: Whether this batch uses the explicit primary-key template.

        Returns:
            The SQL.
        """
        conflict = ConflictClause.get_conflict(bulk_create)
        if conflict is None and not omit_fields:
            templates = bulk_create._writer.templates
            return templates.explicit_key_sql if primary_key_given else templates.insert_sql
        returning = (
            ReturnedRowsMatching.get_returning_columns(bulk_create, omit_fields) if bulk_create._returning else ()
        )
        return TemplateInsert.get_row_template(bulk_create, None, returning)[0]
