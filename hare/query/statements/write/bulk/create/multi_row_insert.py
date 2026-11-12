from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.query.execution.write_statements import WriteStatement
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.bulk_create_reports import BulkCreateReports
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.query.statements.write.bulk.create.declarations import BoundInsertBatch
from hare.query.statements.write.bulk.create.inline_insert import InlineInsert
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.query.statements.write.bulk.create.template_insert import TemplateInsert
from hare.sql.terms.parameters.query_parameters import QueryParameters
from hare.sql.terms.term import Term
from hare.sql.terms.values.literal_value import LiteralValue

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class MultiRowInsert:
    """One INSERT of many rows per batch - a parameter per value, or an array per column where the
    dialect binds one - kept under the backend's bind-parameter ceiling, reading the written rows
    back when asked."""

    @staticmethod
    def bind_batches(
        bulk_create: BulkCreateQuery[Any],
        db_columns: list[str],
        field_names: list[str],
        objects: list[TModel],
        omit_fields: set[str],
        populate_returned_fields: bool = False,
        column_arrays_source_sql: str | None = None,
        *,
        parameters_inline: bool = False,
    ) -> list[BoundInsertBatch]:
        """Binds the objects into multi-row INSERTs, batched by the database's limit of parameters - the
        values the database writes read back when asked.

        Args:
            bulk_create: The insert.
            db_columns: The columns written.
            field_names: Their fields.
            objects: The objects.
            omit_fields: The fields left to the database.
            populate_returned_fields: Whether the values the database writes are read back.
            column_arrays_source_sql: The source of the rows as column arrays, where the database takes them.
            parameters_inline: Retain literal SQL from these same converted rows for a preview.
        """
        # An array per column binds as many parameters whatever the row count.
        batch_size = (
            bulk_create._batch_size
            if column_arrays_source_sql is not None
            else MultiRowInsert.get_multi_row_batch_size(bulk_create, db_columns)
        )
        returning_columns = (
            ReturnedRowsMatching.get_returning_columns(bulk_create, omit_fields) if populate_returned_fields else []
        )
        # ON CONFLICT may skip rows, so the returned rows no longer line up with the objects - the
        # conflict target's columns are returned too, to match them by value.
        conflict_field_names = (
            list(bulk_create._on_conflict)
            if (ConflictClause.may_skip_conflicting_rows(bulk_create) and bulk_create._on_conflict)
            else []
        )
        # An upsert's row of a conflicting object keeps the key it had - where the database returns rows
        # in no order, they are matched back by the conflict target as well.
        if populate_returned_fields and bulk_create._on_conflict:
            conflict_source_columns = [
                bulk_create.model._meta.fields_db_projection[field_name] for field_name in bulk_create._on_conflict
            ]
            returning_columns = list(dict.fromkeys([*returning_columns, *conflict_source_columns]))
        # Rows of an insert skipping conflicts that come in no order are told apart by every column written.
        written_field_names = field_names if populate_returned_fields and bulk_create._ignore_conflicts else []
        # The columns set on the objects - not those returned only to match the rows.
        applied_columns = list(returning_columns)
        if written_field_names and not bulk_create.dialect.features.guarantees_returning_order:
            returning_columns = list(dict.fromkeys([*returning_columns, *db_columns]))
        statement_objects = [
            statement_objects_item
            for objects_item in BulkWriteBatches.get_batches(objects, batch_size)
            for statement_objects_item in ConflictClause.split_by_conflict_key(bulk_create, list(objects_item))
        ]
        sensitive_positions = (
            BulkWriteBatches.get_sensitive_positions(bulk_create.model, field_names)
            if bulk_create.model._meta.sensitive_fields
            else []
        )
        batches: list[BoundInsertBatch] = []
        for objects_item in statement_objects:
            if not objects_item:
                continue
            rows = BulkWriteBatches.serialize_instances(
                bulk_create.model,
                bulk_create._connection.dialect.types,
                objects_item,
                field_names,
            )
            returns_rows = populate_returned_fields or BulkCreateReports.reports_returned_rows(bulk_create)
            sql, conflict_values = MultiRowInsert.get_multi_row_template(
                bulk_create,
                db_columns,
                len(rows),
                returning_columns if returns_rows else None,
                column_arrays_source_sql,
            )
            values: Any
            if column_arrays_source_sql is not None:
                values = [list(column) for column in zip(*rows or (), strict=True)]
                values.extend(conflict_values)
            else:
                values = [value for row in rows or () for value in row]
                values.extend(conflict_values)
            if sensitive_positions:
                # A parameter per value, or one per column holding the column's values.
                hidden_positions = (
                    sensitive_positions
                    if column_arrays_source_sql is not None
                    else [
                        row_index * len(field_names) + position
                        for row_index in range(len(rows))
                        for position in sensitive_positions
                    ]
                )
                values = QueryParameters(values, hidden_positions)
            inline_statements: tuple[str, ...] = ()
            if parameters_inline:
                inline_source = (
                    MultiRowInsert.get_column_arrays_rows_source_sql(
                        bulk_create, field_names, column_values=[list(column) for column in zip(*rows, strict=True)]
                    )
                    if column_arrays_source_sql is not None
                    else None
                )
                inline_statements = (
                    InlineInsert.render(
                        bulk_create,
                        db_columns,
                        rows,
                        returning=(
                            [*returning_columns, *BulkCreateReports.get_reported_returning_terms(bulk_create)]
                            if returns_rows
                            else ()
                        ),
                        rows_source_sql=inline_source,
                    ),
                )
            batches.append(
                BoundInsertBatch(
                    WriteStatement(sql, values, returns_rows),
                    objects_item,
                    populate_returned_fields,
                    tuple(conflict_field_names),
                    tuple(written_field_names),
                    tuple(applied_columns),
                    inline_statements,
                )
            )
        return batches

    @staticmethod
    def get_multi_row_template(
        bulk_create: BulkCreateQuery[Any],
        db_columns: list[str],
        row_count: int,
        returning_columns: list[str] | None,
        column_arrays_source_sql: str | None = None,
    ) -> tuple[str, list[Any]]:
        """The multi-row INSERT of this call's ``ON CONFLICT`` clause for ``row_count`` rows, kept in
        ``StatementPlans.insert_templates``.

        Args:
            bulk_create: The bulk insert.
            db_columns: Each row's columns.
            row_count: How many rows the INSERT writes.
            returning_columns: The columns it returns - with the terms the change report reads -
                None for no ``RETURNING``.
            column_arrays_source_sql: The dialect's source of rows read from an array per column -
                the statement then fits any row count; None for ``VALUES``.

        Returns:
            The SQL text and the values of the parameters following the rows'.
        """
        conflict = ConflictClause.get_conflict(bulk_create)
        meta = bulk_create.model._meta
        key = (
            bulk_create.model,
            bulk_create._connection.connection_alias,
            bulk_create._connection.dialect,
            meta.query_class,
            meta.schema,
            meta.db_table,
            "rows",
            tuple(db_columns),
            row_count if column_arrays_source_sql is None else column_arrays_source_sql,
            conflict,
            None
            if returning_columns is None
            else (tuple(returning_columns), BulkCreateReports.reports_returned_rows(bulk_create)),
        )
        cacheable = True
        try:
            template = StatementPlans.insert_templates.get_bucket(bulk_create.model).get(key)
        except TypeError:
            # A tenant value the conflict update is limited to that can't be hashed - built each time.
            cacheable = False
            template = None
        if template is None and column_arrays_source_sql is not None:
            query = bulk_create._get_insert_statement().get_query(
                db_columns,
                (),
                conflict=conflict,
                returning=(
                    ()
                    if returning_columns is None
                    else [*returning_columns, *BulkCreateReports.get_reported_returning_terms(bulk_create)]
                ),
                rows_source_sql=column_arrays_source_sql,
            )
            template = TemplateInsert.get_row_template_sql(query, len(db_columns))
            if cacheable:
                StatementPlans.insert_templates.get_bucket(bulk_create.model)[key] = template
        if template is None:
            # A row's placeholders are one string in one LiteralValue term, not a Parameter per value.
            dialect = bulk_create._connection.dialect
            column_count = len(db_columns)
            parameter_rows = [
                [
                    LiteralValue(
                        ",".join(
                            dialect.parameters.get_placeholder(index)
                            for index in range(1 + row_index * column_count, 1 + (row_index + 1) * column_count)
                        )
                    )
                ]
                for row_index in range(row_count)
            ]
            query = bulk_create._get_insert_statement().get_query(
                db_columns,
                parameter_rows,
                conflict=conflict,
                returning=(
                    ()
                    if returning_columns is None
                    else [*returning_columns, *BulkCreateReports.get_reported_returning_terms(bulk_create)]
                ),
            )
            template = TemplateInsert.get_row_template_sql(query, row_count * column_count)
            if cacheable:
                StatementPlans.insert_templates.get_bucket(bulk_create.model)[key] = template
        return template

    @staticmethod
    def get_multi_row_batch_size(bulk_create: BulkCreateQuery[Any], db_columns: list[str]) -> int:
        """Objects per multi-row ``INSERT``, kept under the backend's bind-parameter ceiling
        together with the parameters the statement binds besides its rows.

        Args:
            bulk_create: The bulk insert.
            db_columns: The INSERT's columns.

        Returns:
            The batch size.
        """
        max_bind_parameters = (
            bulk_create._connection.features.max_bind_parameters - bulk_create._get_fixed_parameter_count(db_columns)
        )
        return BulkWriteBatches.get_bind_parameter_safe_batch_size(
            bulk_create._batch_size, len(db_columns), max_bind_parameters
        )

    @staticmethod
    def get_column_arrays_rows_source_sql(
        bulk_create: BulkCreateQuery[Any], field_names: list[str], *, column_values: list[list[Any]] | None = None
    ) -> str | None:
        """The dialect's source of the rows of a multi-row INSERT binding one array per column -
        None where the dialect binds a parameter per value, or a column's type isn't known.

        Args:
            bulk_create: The bulk insert.
            field_names: The written fields, in column order.
            column_values: Bound column arrays to render as literals, only for a preview.

        Returns:
            The SQL, or None.
        """
        dialect = bulk_create._connection.dialect
        fields_map = bulk_create.model._meta.fields_map
        cast_types = [
            dialect.parameters.get_field_parameter_cast_type(fields_map[field_name]) for field_name in field_names
        ]
        if not cast_types or any(cast_type is None for cast_type in cast_types):
            return None
        if column_values is None:
            return dialect.parameters.get_column_arrays_rows_source_sql(cast_types, 1)  # type: ignore[arg-type]
        context = bulk_create._connection.query_class.SQL_CONTEXT
        return dialect.parameters.get_column_arrays_rows_source_sql(
            cast_types,  # type: ignore[arg-type]
            1,
            column_values_sql=[Term.wrap_constant(values).get_sql(context) for values in column_values],
        )
