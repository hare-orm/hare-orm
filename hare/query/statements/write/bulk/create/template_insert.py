from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.terms.parameters.parameterizer import Parameterizer

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
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
        meta = bulk_create.model._meta
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
        cacheable = True
        try:
            template = StatementPlans.insert_templates.get(key)
        except TypeError:
            # A tenant value the conflict update is limited to that can't be hashed - built each time.
            cacheable = False
            template = None
        if template is None:
            insert_statement = bulk_create._get_insert_statement()
            if columns is None:
                template = (str(insert_statement.get_query([], [], returning=returning)), [])
            else:
                query = insert_statement.get_query(
                    columns, [insert_statement.get_parameter_row(len(columns))], conflict=conflict, returning=returning
                )
                template = TemplateInsert.get_row_template_sql(query, len(columns))
            if cacheable:
                StatementPlans.insert_templates[key] = template
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
    def make_template_statements(
        bulk_create: BulkCreateQuery[Any], omit_fields: set[str] | None = None
    ) -> tuple[tuple[str, list[Any]], tuple[str, list[Any]]]:
        """The single-row INSERT templates of objects without and with a caller-given primary
        key, each with the values of the parameters following the row's own.

        Args:
            bulk_create: The bulk insert.
            omit_fields: Fields left to their database default.

        Returns:
            ``(sql, values)`` of both templates.
        """
        if omit_fields is None:
            omit_fields = set()
        conflict = ConflictClause.get_conflict(bulk_create)
        if conflict is None and not omit_fields:
            return (bulk_create._writer.insert_query, []), (bulk_create._writer.insert_query_all, [])

        insert_statement = bulk_create._get_insert_statement()
        templates: list[tuple[str, list[Any]]] = []
        for primary_key_given in (False, True):
            columns = bulk_create._filter_columns(omit_fields, include_generated=primary_key_given)
            if not columns:
                default_sql = TemplateInsert.build_default_values_sql(bulk_create, omit_fields)
                return (default_sql, []), (default_sql, [])
            templates.append(
                TemplateInsert.get_row_template(
                    bulk_create, columns, insert_statement.get_returning_columns(primary_key_given=primary_key_given)
                )
            )
        return templates[0], templates[1]

    @staticmethod
    async def execute_via_execute_many(
        bulk_create: BulkCreateQuery[Any],
        db_columns: list[str],
        field_names: list[str],
        objects: list[TModel],
    ) -> None:
        sql, statement_values = TemplateInsert.get_row_template(bulk_create, db_columns)
        for objects_item in BulkWriteBatches.get_batches(objects, bulk_create._batch_size):
            objects_item = list(objects_item)
            if not objects_item:
                continue
            parameter_rows = BulkWriteBatches.write_statement_parameter_rows(
                bulk_create.model, bulk_create._connection, objects_item, field_names, statement_values
            )
            if parameter_rows is not None:
                await bulk_create._connection.execute_many(sql, parameter_rows)
                continue
            rows = BulkWriteBatches.serialize_instances(
                bulk_create.model, bulk_create._connection.dialect.types, objects_item, field_names
            )
            if statement_values:
                rows = [[*row, *statement_values] for row in rows]
            if bulk_create.model._meta.sensitive_fields:
                rows = BulkWriteBatches.hide_row_values(
                    rows, BulkWriteBatches.get_sensitive_positions(bulk_create.model, field_names)
                )
            await bulk_create._connection.execute_many(sql, rows)

    @staticmethod
    def build_default_values_sql(bulk_create: BulkCreateQuery[Any], omit_fields: set[str] | None = None) -> str:
        """The ``DEFAULT VALUES`` INSERT of one row - every column left to the database - returning
        its columns when ``returning=True``.

        Args:
            bulk_create: The bulk insert.
            omit_fields: Fields left out because every object relies on its database default.

        Returns:
            The SQL.
        """
        returning = (
            ReturnedRowsMatching.get_returning_columns(bulk_create, omit_fields or ())
            if bulk_create._returning
            else ()
        )
        return TemplateInsert.get_row_template(bulk_create, None, returning)[0]
