from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from hare.query.statements.write.bulk.create.bulk_create_reports import BulkCreateReports
from hare.query.statements.write.bulk.create.conflict_clause import ConflictClause
from hare.query.statements.write.bulk.create.returned_rows_matching import ReturnedRowsMatching
from hare.query.statements.write.bulk.create.template_insert import TemplateInsert
from hare.sql.terms.parameters.query_parameters import QueryParameters
from hare.sql.terms.values.literal_value import LiteralValue

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class MultiRowInsert:
    """One INSERT of many rows per batch - a parameter per value, or an array per column where the
    dialect binds one - kept under the backend's bind-parameter ceiling, reading the written rows
    back when asked."""

    @staticmethod
    async def execute_via_multi_row_statement(
        bulk_create: BulkCreateQuery[Any],
        db_columns: list[str],
        field_names: list[str],
        objects: list[TModel],
        omit_fields: set[str],
        populate_returned_fields: bool = False,
        column_arrays_source_sql: str | None = None,
    ) -> None:
        # An array per column binds as many parameters whatever the row count.
        batch_size = (
            bulk_create._batch_size
            if column_arrays_source_sql is not None
            else MultiRowInsert.get_multi_row_batch_size(bulk_create, db_columns, omit_fields)
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
        if populate_returned_fields and conflict_field_names:
            conflict_source_columns = [
                bulk_create.model._meta.fields_db_projection[field_name] for field_name in conflict_field_names
            ]
            returning_columns = list(dict.fromkeys([*returning_columns, *conflict_source_columns]))
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
        for objects_item in statement_objects:
            if not objects_item:
                continue
            if column_arrays_source_sql is not None:
                parameters = BulkWriteBatches.write_statement_column_parameters(
                    bulk_create.model, bulk_create._connection, objects_item, field_names
                )
            else:
                parameters = BulkWriteBatches.write_statement_parameters(
                    bulk_create.model, bulk_create._connection, objects_item, field_names, []
                )
            rows = (
                BulkWriteBatches.serialize_instances(
                    bulk_create.model, bulk_create._connection.dialect.types, objects_item, field_names
                )
                if parameters is None
                else None
            )
            returns_rows = populate_returned_fields or BulkCreateReports.reports_returned_rows(bulk_create)
            sql, conflict_values = MultiRowInsert.get_multi_row_template(
                bulk_create,
                db_columns,
                len(objects_item) if rows is None else len(rows),
                returning_columns if returns_rows else None,
                column_arrays_source_sql,
            )
            values: Any
            if parameters is not None:
                values = parameters.extended(conflict_values) if conflict_values else parameters
            elif column_arrays_source_sql is not None:
                values = [list(column) for column in zip(*rows or (), strict=True)]
                values.extend(conflict_values)
            else:
                values = [value for row in rows or () for value in row]
                values.extend(conflict_values)
            if sensitive_positions and rows is not None:
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
            __, returned_rows = await bulk_create._connection.execute(sql, values, returns_rows=returns_rows)
            if populate_returned_fields:
                ReturnedRowsMatching.populate_returned_fields_from_returning_rows(
                    bulk_create, objects_item, returned_rows, conflict_field_names
                )
            if bulk_create._reported_rows is not None:
                bulk_create._reported_rows.extend(dict(row) for row in returned_rows)

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
            template = StatementPlans.insert_templates.get(key)
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
                StatementPlans.insert_templates[key] = template
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
                StatementPlans.insert_templates[key] = template
        return template

    @staticmethod
    def get_multi_row_batch_size(
        bulk_create: BulkCreateQuery[Any], db_columns: list[str], omit_fields: set[str]
    ) -> int:
        """Objects per multi-row ``INSERT``, kept under the backend's bind-parameter ceiling
        together with the parameters the statement binds besides its rows.

        Args:
            bulk_create: The bulk insert.
            db_columns: The INSERT's columns.
            omit_fields: Fields left out of the INSERT because every object relies on its DB default.

        Returns:
            The batch size.
        """
        max_bind_parameters = (
            bulk_create._connection.features.max_bind_parameters
            - bulk_create._get_fixed_parameter_count(db_columns, omit_fields)
        )
        return BulkWriteBatches.get_bind_parameter_safe_batch_size(
            bulk_create._batch_size, len(db_columns), max_bind_parameters
        )

    @staticmethod
    def get_column_arrays_rows_source_sql(bulk_create: BulkCreateQuery[Any], field_names: list[str]) -> str | None:
        """The dialect's source of the rows of a multi-row INSERT binding one array per column -
        None where the dialect binds a parameter per value, or a column's type isn't known.

        Args:
            bulk_create: The bulk insert.
            field_names: The written fields, in column order.

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
        return dialect.parameters.get_column_arrays_rows_source_sql(cast_types, 1)  # type: ignore[arg-type]
