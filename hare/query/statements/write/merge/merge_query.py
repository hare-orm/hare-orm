from __future__ import annotations

import functools
import operator
from collections.abc import Generator, Mapping, Sequence
from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.clauses.enums import MergeAction, MergeMatch
from hare.dialects.base.clauses.merge_when_sql import MergeWhenSql
from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.relation_values import RelationValues
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.enums import RowOperation
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.update.expression_assignment import ExpressionAssignment
from hare.models.write.write_steps import WriteSteps
from hare.query.constants import MERGE_SOURCE_ALIAS, MERGE_SOURCE_KEY
from hare.query.enums import RowShape
from hare.query.expressions.conditions.q import Q
from hare.query.expressions.expression import Expression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.joins.merge_source_row import MergeSourceRow
from hare.query.expressions.subqueries.subquery import Subquery
from hare.query.query_connection import QueryConnection
from hare.query.scopes.row_scopes import RowScopes
from hare.query.statements.constants import (
    MERGE_ACTION_ALIAS,
    MERGE_ACTION_KEY,
    MERGE_VALUES_ALIAS,
    ROW_OPERATION_BY_MERGE_ACTION,
)
from hare.query.statements.write.field_columns import FieldColumns
from hare.query.statements.write.merge.merge_when import MergeWhen
from hare.query.statements.write.returning.returned_rows import ReturnedRows
from hare.sql import Table
from hare.sql.builder.returned_value import ReturnedValue
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.criteria.empty_criterion import EmptyCriterion
from hare.sql.terms.parameters.parameterizer import Parameterizer
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.sql.sql_context import SqlContext


class MergeQuery:
    """``Model.objects.merge(source, on=...)``: one ``MERGE`` statement matching the source rows to the
    model's rows and, branch by branch, updating, deleting or inserting them. Awaits to the number of
    rows written; with ``returning(...)`` to the rows.

    Args:
        target: The queryset of the written model - its filters and default scope limit the rows a
            source row matches; its connection and tenant visibility.
        source: The rows - a ``values()`` queryset, a queryset of models (all its fields), a union of
            ``values()`` querysets, or a list of dicts keyed by field names of the model.
        on: The fields matched - a name or names the source has a column of too, or a dict of the
            model's field to the source column.

    Raises:
        QueryError: ``source`` or ``on`` is of another type; a list's rows have different keys.
        FieldError: A field of ``on`` isn't stored in the model's table.
    """

    __slots__ = ("target", "source", "on_columns", "whens", "returned_rows")

    def __init__(self, target: QuerySet[Any, Any], source: Any, on: str | Sequence[str] | Mapping[str, str]) -> None:
        self.target = target
        self.source: QuerySet[Any, Any] | tuple[Mapping[str, Any], ...] = self.get_source(source)
        model = target.model
        #: The target column and the source column of each matched pair.
        self.on_columns: tuple[tuple[str, str], ...] = tuple(
            (FieldColumns.get_column_field(model, target_name, "merge(on=...)")[0], source_column)
            for target_name, source_column in self.get_on_pairs(on)
        )
        self.whens: tuple[MergeWhen, ...] = ()
        self.returned_rows: ReturnedRows | None = None

    @staticmethod
    def get_source(source: Any) -> QuerySet[Any, Any] | tuple[Mapping[str, Any], ...]:
        """The source rows as the merge reads them.

        Raises:
            QueryError: The source is neither a queryset nor a list of dicts of the same keys, or is a
                ``values_list()`` queryset - its columns have no names.
        """
        from hare.query.queryset.queryset import QuerySet

        if isinstance(source, QuerySet):
            if source._combination is not None:
                return source
            if source._selection is None:
                return source.values()
            if source._selection.shape is not RowShape.DICT:
                raise QueryError("merge() reads its source columns by name - give a values() queryset")
            return source
        if isinstance(source, (list, tuple)) and all(isinstance(row, Mapping) for row in source):
            if source and any(set(row) != set(source[0]) or not row for row in source):
                raise QueryError("merge() takes a list of rows of the same keys")
            return tuple(source)
        raise QueryError(f"merge() takes a queryset or a list of dicts as its source, got {source!r}")

    @staticmethod
    def get_on_pairs(on: Any) -> list[tuple[str, str]]:
        """The ``(target field, source column)`` pairs ``on`` names.

        Raises:
            QueryError: ``on`` is empty or of another type.
        """
        if isinstance(on, str):
            return [(on, on)]
        if (
            isinstance(on, Mapping)
            and on
            and all(isinstance(key, str) and isinstance(value, str) for key, value in on.items())
        ):
            return list(on.items())
        if isinstance(on, (list, tuple)) and on and all(isinstance(name, str) for name in on):
            return [(name, name) for name in on]
        raise QueryError(f"merge(on=...) takes field names or a dict of them to source columns, got {on!r}")

    def get_copy(self, *, when: MergeWhen | None = None, returned_rows: ReturnedRows | None = None) -> MergeQuery:
        """A copy with one more branch or with the returned fields.

        Returns:
            The copy.
        """
        merge_copy = copy(self)
        if when is not None:
            merge_copy.whens = (*self.whens, when)
        if returned_rows is not None:
            merge_copy.returned_rows = returned_rows
        return merge_copy

    def when_matched(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        delete: bool = False,
        do_nothing: bool = False,
        condition: Q | None = None,
    ) -> MergeQuery:
        """A branch for the model's rows a source row matches - updated, deleted or left alone.

        Args:
            update: The value of each field set - plain values, ``F("<field>")`` of the row,
                ``F("merge_source__<column>")`` of the source row, expressions of them.
            delete: Delete the rows.
            do_nothing: Leave the rows alone - and keep the later branches from taking them.
            condition: The rows the branch takes - a ``Q`` over the row and ``merge_source__<column>``.

        Returns:
            The merge with the branch after the others.

        Raises:
            QueryError: Not exactly one action is given, or an argument is of another type.
        """
        actions = {MergeAction.UPDATE: update, MergeAction.DELETE: delete, MergeAction.DO_NOTHING: do_nothing}
        return self.get_copy(when=MergeWhen.build("when_matched", MergeMatch.MATCHED, actions, condition))

    def when_not_matched(
        self,
        *,
        insert: Mapping[str, Any] | None = None,
        do_nothing: bool = False,
        condition: Q | None = None,
    ) -> MergeQuery:
        """A branch for the source rows no row of the model matches - inserted or left out.

        Args:
            insert: The value of each field of the new row - plain values,
                ``F("merge_source__<column>")``, expressions of them.
            do_nothing: Leave the source rows out - and keep the later branches from taking them.
            condition: The source rows the branch takes - a ``Q`` over ``merge_source__<column>``.

        Returns:
            The merge with the branch after the others.

        Raises:
            QueryError: Not exactly one action is given, or an argument is of another type.
        """
        actions = {MergeAction.INSERT: insert, MergeAction.DO_NOTHING: do_nothing}
        return self.get_copy(when=MergeWhen.build("when_not_matched", MergeMatch.NOT_MATCHED, actions, condition))

    def when_not_matched_by_source(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        delete: bool = False,
        do_nothing: bool = False,
        condition: Q | None = None,
    ) -> MergeQuery:
        """A branch for the model's rows (of the target queryset) no source row matches - updated,
        deleted or left alone. PostgreSQL 17+.

        Args:
            update: The value of each field set - plain values, ``F()`` of the row, expressions.
            delete: Delete the rows.
            do_nothing: Leave the rows alone.
            condition: The rows the branch takes, a ``Q`` over the row.

        Returns:
            The merge with the branch after the others.

        Raises:
            QueryError: Not exactly one action is given, or an argument is of another type.
        """
        actions = {MergeAction.UPDATE: update, MergeAction.DELETE: delete, MergeAction.DO_NOTHING: do_nothing}
        return self.get_copy(
            when=MergeWhen.build("when_not_matched_by_source", MergeMatch.NOT_MATCHED_BY_SOURCE, actions, condition)
        )

    def returning(self, *field_names: str, old: Sequence[str] = ()) -> MergeQuery:
        """The merge returning each row it wrote - a dict of ``field_names`` and, under
        ``"merge_action"``, the action that wrote it (``"insert"``, ``"update"``, ``"delete"``); a
        deleted row as it was. PostgreSQL 17+. With ``old``, each dict also holds those fields as they
        were before the merge, under ``"old"`` - None for an inserted row (PostgreSQL 18+).

        Args:
            field_names: The fields - a concrete field, a forward relation (its key) or ``pk``.
            old: The fields returned as they were before the merge too.

        Returns:
            The merge, its result the list of rows.

        Raises:
            QueryError: No field is named.
            FieldError: A name isn't a field written in the model's table, or is given twice.
        """
        if not field_names:
            raise QueryError("merge().returning() takes the names of the fields returned")
        return self.get_copy(returned_rows=ReturnedRows(self.target.model, field_names, old))

    def __await__(self) -> Generator[Any, None, Any]:
        return self._execute().__await__()

    def raise_if_unmergeable(self, connection: DatabaseClient) -> None:
        """Rejects what the merge can't run - before any SQL.

        Raises:
            QueryError: No branch; the target queryset does more than filter; a delete the database
                can't carry out alone.
            UnSupportedError: The database has no ``MERGE``, ``WHEN NOT MATCHED BY SOURCE`` or
                ``MERGE ... RETURNING``.
        """
        target = self.target
        model = target.model
        features = connection.features
        if not features.supports_merge:
            raise UnSupportedError(f"merge() needs MERGE, which {connection.dialect} doesn't have")
        if any(when.match is MergeMatch.NOT_MATCHED_BY_SOURCE for when in self.whens) and (
            not features.supports_merge_not_matched_by_source
        ):
            raise UnSupportedError(f"when_not_matched_by_source() needs a MERGE {connection.dialect} doesn't have")
        if self.returned_rows is not None and not features.supports_merge_returning:
            raise UnSupportedError(
                f"merge().returning() needs MERGE ... RETURNING, which {connection.dialect} doesn't have"
            )
        if (
            self.returned_rows is not None
            and self.returned_rows.old_source_names_by_name
            and not features.supports_returning_old_new
        ):
            raise UnSupportedError(
                f"merge().returning(old=...) needs RETURNING of the old values, which the {connection.dialect} server "
                "of this connection doesn't have"
            )
        capture_needs = model._meta.change_capture_needs
        if capture_needs is not None and not features.supports_merge_returning:
            raise UnSupportedError(
                f"merge() of {model.__name__}, which captures its changes (Meta.change_capture), needs MERGE ... "
                f"RETURNING, which {connection.dialect} doesn't have"
            )
        if capture_needs is not None and capture_needs.reads_before and not features.supports_returning_old_new:
            raise UnSupportedError(
                f"merge() of {model.__name__}, which captures its rows as they were (ChangePayload.BEFORE_AND_AFTER), "
                f"needs RETURNING of the old values, which the {connection.dialect} server of this connection doesn't "
                "have"
            )
        if not self.whens:
            raise QueryError(
                "merge() needs a branch - when_matched(), when_not_matched() or when_not_matched_by_source()"
            )
        if (
            target._annotations
            or target._limit is not None
            or target._offset
            or target._distinct
            or target._distinct_on
            or target._group_bys
            or target._table_sample is not None
            or target._with_ctes
        ):
            raise QueryError(
                "merge() takes a target queryset that only filters the rows - no annotations, slice, distinct(), "
                "group_by(), sample() or with_cte()"
            )
        if any(when.action is MergeAction.DELETE for when in self.whens) and (
            model._meta.soft_delete_field
            or DeletionGraph.needs_python_cascade(model)
            or DeletionGraph.has_protecting_relations(model)
            or DeletionGraph.has_transitive_protect(model)
        ):
            raise QueryError(
                f"merge() deletes {model.__name__} rows in the database alone - its soft delete, an on_delete the "
                "database doesn't enforce, a PROTECT or the capture of changed rows (Meta.change_capture) need "
                "delete()"
            )

    async def _execute(self) -> Any:
        """Builds and runs the ``MERGE``, and reports it.

        Returns:
            The number of rows written, or the returned rows.
        """
        model = self.target.model
        meta = model._meta
        connection = self.target._get_execution_query(for_write=True)._connection
        self.raise_if_unmergeable(connection)
        if not self.source:
            return [] if self.returned_rows is not None else 0
        if MERGE_SOURCE_KEY in meta.fields_map:
            raise QueryError(f"merge() reads its source row as {MERGE_SOURCE_KEY!r}, a field of {model.__name__} too")
        sql_context = connection.query_class.SQL_CONTEXT.copy(parameterizer=Parameterizer(), with_namespace=True)
        table = meta.basetable
        source_sql, fields_by_column = self.get_source_sql(connection, sql_context)
        expression_context = ExpressionContext(
            model=model,
            dialect=connection.dialect,
            connection=connection,
            table=table,
            annotations={MERGE_SOURCE_KEY: MergeSourceRow(fields_by_column).with_name(MERGE_SOURCE_KEY)},
            visibility=self.target._visibility,
        )
        on_sql = self.get_on_criterion(expression_context).get_sql(sql_context)
        relation_rows: list[dict[str, Any]] = []
        whens_sql = [self.get_when_sql(when, expression_context, sql_context, relation_rows) for when in self.whens]
        if relation_rows:
            await Tenancy.check_relation_targets(model, relation_rows, connection)
        returned = (
            [
                *(
                    ReturnedValue(
                        sql=table[column].get_sql(sql_context), alias_sql=sql_context.quote(column), column_name=column
                    )
                    for column in self.returned_rows.get_returning_columns()
                ),
                *(
                    ReturnedValue(
                        sql=term.get_sql(sql_context), alias_sql=sql_context.quote(term.alias), column_name=term.alias
                    )
                    for term in self.returned_rows.get_old_value_terms()
                ),
            ]
            if self.returned_rows is not None
            else []
        )
        capture_needs = meta.change_capture_needs
        if capture_needs is not None:
            # The written rows' keys and captured fields, as they were too for a payload holding both.
            returned.extend(
                ReturnedValue(
                    sql=table[column].get_sql(sql_context), alias_sql=sql_context.quote(column), column_name=column
                )
                for column in capture_needs.columns
            )
            if capture_needs.reads_before:
                returned.extend(
                    ReturnedValue(
                        sql=term.get_sql(sql_context), alias_sql=sql_context.quote(term.alias), column_name=term.alias
                    )
                    for term in ChangeCapturing.get_old_value_terms(model, capture_needs)
                )
        sql = connection.dialect.clauses.get_merge_sql(
            target_sql=table.get_sql(sql_context),
            source_sql=source_sql,
            source_alias_sql=sql_context.quote(MERGE_SOURCE_ALIAS),
            on_sql=on_sql,
            whens=whens_sql,
            returned=returned,
            action_alias_sql=sql_context.quote(MERGE_ACTION_ALIAS),
        )
        parameters = cast("Parameterizer", sql_context.parameterizer).values
        if capture_needs is not None:
            return await self.execute_captured(connection, sql, parameters, capture_needs)
        if self.returned_rows is None:
            count = (await connection.execute(sql, parameters, returns_rows=False))[0]
            result: Any = count
        else:
            count, raw_rows = await connection.execute(sql, parameters, returns_rows=True)
            result = self.returned_rows.get_rows(connection.dialect.types, raw_rows)
            for row, raw_row in zip(result, raw_rows, strict=True):
                row[MERGE_ACTION_KEY] = str(dict(raw_row)[MERGE_ACTION_ALIAS]).lower()
        if count:
            for operation in dict.fromkeys(
                ROW_OPERATION_BY_MERGE_ACTION[when.action]
                for when in self.whens
                if when.action in ROW_OPERATION_BY_MERGE_ACTION
            ):
                await WriteSteps.report(connection, model, operation)
        return result

    async def execute_captured(
        self, connection: DatabaseClient, sql: str, parameters: list[Any], capture_needs: CaptureNeeds
    ) -> Any:
        """Runs the ``MERGE`` of a model with ``Meta.change_capture`` and captures the rows it wrote,
        by the action of each, in one transaction.

        Args:
            connection: The connection.
            sql: The ``MERGE``, returning the captured columns and the action.
            parameters: Its parameters.
            capture_needs: The model's needs.

        Returns:
            The number of rows written, or the returned rows.
        """
        model = self.target.model
        async with ChangeCapturing.transaction(connection) as transaction_connection:
            count, raw_rows = await transaction_connection.execute(sql, parameters, returns_rows=True)
            rows_by_operation: dict[RowOperation, list[Any]] = {}
            for raw_row in raw_rows:
                operation = RowOperation(str(dict(raw_row)[MERGE_ACTION_ALIAS]).lower())
                rows_by_operation.setdefault(operation, []).append(raw_row)
            changes = [
                change
                for operation, operation_rows in rows_by_operation.items()
                for change in ChangeCapturing.get_returned_changes(
                    model,
                    capture_needs,
                    transaction_connection.dialect.types,
                    operation_rows,
                    operation,
                    returns_old=capture_needs.reads_before and operation is RowOperation.UPDATE,
                )
            ]
            await ChangeCapturing.capture(transaction_connection, model, changes)
        for operation in rows_by_operation:
            await WriteSteps.report(
                connection,
                model,
                operation,
                pks=[change.pk for change in changes if change.operation is operation],
            )
        if self.returned_rows is None:
            return count
        result = self.returned_rows.get_rows(connection.dialect.types, raw_rows)
        for row, raw_row in zip(result, raw_rows, strict=True):
            row[MERGE_ACTION_KEY] = str(dict(raw_row)[MERGE_ACTION_ALIAS]).lower()
        return result

    def get_source_sql(
        self, connection: DatabaseClient, sql_context: SqlContext
    ) -> tuple[str, dict[str, Field[Any] | None]]:
        """The source rows in parentheses - the queryset's subquery, or a ``VALUES`` table of the list
        with each column cast to its field's type.

        Args:
            connection: The connection.
            sql_context: The context the statement renders in.

        Returns:
            The SQL, and the source's columns with the field each one's values are of.
        """
        model = self.target.model
        source = self.source
        if not isinstance(source, tuple):
            source_query = QueryConnection.get_bound_to(
                source._get_compiler(), connection, model, "the source of merge()"
            )
            source_query._make_subquery()
            subquery = Subquery(source_query)
            fields_by_column = {
                name: subquery.get_selected_field(name) for name in subquery.get_selected_names() or []
            }
            return source_query.query.get_sql(sql_context.copy(subquery=True, with_alias=False)), fields_by_column
        meta = model._meta
        dialect = connection.dialect
        columns = list(source[0])
        fields: list[Field[Any] | None] = []
        for column in columns:
            # A column named after a field of the model is cast to the field's type; another one is
            # left to the database.
            field = meta.fields_map.get(column)
            if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)) and len(field.source_fields) == 1:
                field = meta.fields_map[field.source_fields[0]]
            fields.append(field if field is not None and field.model_field_name in meta.fields_db_projection else None)
        rows_sql: list[str] = []
        for row in source:
            values_sql: list[str] = []
            for column, field in zip(columns, fields, strict=True):
                value = self.get_written_value(column, row[column])
                if field is None:
                    values_sql.append(ValueWrapper(value).get_sql(sql_context))
                    continue
                parameter_sql = ValueWrapper(dialect.types.get_db_value(field, value, None)).get_sql(sql_context)
                values_sql.append(
                    dialect.clauses.get_typed_placeholder_template(
                        parameter_sql, dialect.parameters.get_field_parameter_cast_type(field)
                    )
                )
            rows_sql.append(f"({', '.join(values_sql)})")
        columns_sql = dialect.clauses.get_values_table_columns_sql([sql_context.quote(column) for column in columns])
        values_table_sql = f"(VALUES {', '.join(rows_sql)}) AS {sql_context.quote(MERGE_VALUES_ALIAS)}"
        return f"(SELECT {columns_sql} FROM {values_table_sql})", dict(zip(columns, fields, strict=True))  # nosec B608

    def get_written_value(self, name: str, value: Any) -> Any:
        """A value as its column holds it - a related instance of a forward relation as the key it is
        referenced by.

        Args:
            name: The field or source column.
            value: The value.

        Returns:
            The value.

        Raises:
            ValidationError: A relation is given an instance of another model.
        """
        from hare.models import Model

        model = self.target.model
        relation = model._meta.fields_map.get(name)
        if isinstance(value, Model) and isinstance(relation, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
            RelationValues.validate_relation_type(model, name, value)
            return getattr(value, relation.to_field_instance.model_field_name)
        return value

    def get_on_criterion(self, expression_context: ExpressionContext) -> Criterion:
        """The condition matching a source row to a target row - the matched columns equal, the target
        row inside the model's default scope and the target queryset's filters.

        Raises:
            QueryError: A filter of the target queryset joins another table.
        """
        table = expression_context.table
        source_table = Table(MERGE_SOURCE_ALIAS)
        parts: list[Criterion] = [
            table[target_column] == source_table[source_column] for target_column, source_column in self.on_columns
        ]
        return self.get_criterion([*parts, *self.get_target_criteria(expression_context)])

    def get_target_criteria(self, expression_context: ExpressionContext) -> list[Criterion]:
        """The conditions of the model's rows the merge reads - the default scope and the target
        queryset's filters.

        Raises:
            QueryError: A filter of the target queryset joins another table.
        """
        criteria: list[Criterion] = []
        scope = RowScopes.of(self.target.model).get_criterion(
            expression_context.table,
            visibility=self.target._visibility,
            dialect=expression_context.dialect,
            connection=expression_context.connection,
        )
        if scope is not None:
            criteria.append(scope)
        criteria.extend(
            self.get_condition_criterion(q_object, expression_context, "the target queryset's filter")
            for q_object in self.target._q_objects
        )
        return criteria

    @staticmethod
    def get_criterion(parts: list[Criterion]) -> Criterion:
        """``parts`` joined by ``AND`` - an empty one left out; ``EmptyCriterion`` for none."""
        parts = [part for part in parts if not isinstance(part, EmptyCriterion)]
        return functools.reduce(operator.and_, parts) if parts else EmptyCriterion()

    @staticmethod
    def get_condition_criterion(condition: Q, expression_context: ExpressionContext, what: str) -> Criterion:
        """A condition over the target row and the source row.

        Raises:
            QueryError: The condition joins another table or reads an aggregate.
        """
        modifier = condition.get_result(expression_context)
        if modifier.joins or not isinstance(modifier.having_criterion, EmptyCriterion):
            raise QueryError(f"merge() reads {what} on the target and source rows alone - it can't cross a relation")
        return modifier.where_criterion

    def get_when_sql(
        self,
        when: MergeWhen,
        expression_context: ExpressionContext,
        sql_context: SqlContext,
        relation_rows: list[dict[str, Any]],
    ) -> MergeWhenSql:
        """A branch rendered - its condition, then the columns it writes and their values.

        Args:
            when: The branch.
            expression_context: The context of the target model.
            sql_context: The context the statement renders in.
            relation_rows: Where the plain relation values written are put for the tenant check.

        Returns:
            The branch.
        """
        conditions = (
            []
            if when.condition is None
            else [self.get_condition_criterion(when.condition, expression_context, "a branch's condition")]
        )
        if when.match is MergeMatch.NOT_MATCHED_BY_SOURCE:
            # The rows no source row matches are those of the whole table - limited to the target's here.
            conditions = [*self.get_target_criteria(expression_context), *conditions]
        condition = self.get_criterion(conditions)
        condition_sql = None if isinstance(condition, EmptyCriterion) else condition.get_sql(sql_context)
        if when.action not in {MergeAction.UPDATE, MergeAction.INSERT}:
            return MergeWhenSql(when.match, when.action, condition_sql)
        assignments = self.get_assignments(when, expression_context, relation_rows)
        return MergeWhenSql(
            when.match,
            when.action,
            condition_sql,
            tuple(sql_context.quote(column) for column, _term in assignments),
            tuple(term.get_sql(sql_context) for _column, term in assignments),
        )

    def get_assignments(
        self, when: MergeWhen, expression_context: ExpressionContext, relation_rows: list[dict[str, Any]]
    ) -> list[tuple[str, Term]]:
        """The columns a branch writes and their values - the given ones, the ``auto_now`` moment,
        the version bump of an update, the active tenant of an insert.

        Raises:
            FieldError: A field isn't stored in the model's table, is given twice, or is generated.
            QueryError: An update sets the primary key; a tenant or a relation to a tenant-scoped
                model is written from a value hare can't check under the active scope.
        """
        model = self.target.model
        meta = model._meta
        dialect = expression_context.dialect
        is_insert = when.action is MergeAction.INSERT
        method_name = "when_not_matched(insert=...)" if is_insert else "merge(update=...)"
        values = dict(when.values)
        self.add_tenant_value(values, is_insert)
        now = Timezone.now()
        assignments: list[tuple[str, Term]] = []
        written_columns: set[str] = set()
        relation_row: dict[str, Any] = {}
        for name, value in values.items():
            column, field = FieldColumns.get_column_field(model, name, method_name)
            if column in written_columns:
                raise FieldError(f"{method_name} writes {name!r} twice")
            written_columns.add(column)
            if field.pk and not is_insert:
                raise QueryError(f"merge() can't update the primary key {name!r}")
            relation = meta.fields_map.get(name)
            if isinstance(relation, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                self.raise_if_relation_unchecked(name, relation, value)
                if not isinstance(value, (Expression, Term)):
                    relation_row[field.model_field_name] = self.get_written_value(name, value)
            value = self.get_written_value(name, value)
            value = ExpressionAssignment.get_assigned_value(value)
            term: Term = (
                ExpressionAssignment.get_term(name, field, value, expression_context)
                if isinstance(value, Expression)
                else ValueWrapper(dialect.types.get_db_value(field, value, None))
            )
            assignments.append((column, term))
        for field_name, field in meta.fields_map.items():
            column = meta.fields_db_projection.get(field_name)
            if column is None or column in written_columns or not isinstance(field, (DatetimeField, TimeField)):
                continue
            if field.auto_now or (is_insert and field.auto_now_add):
                assignments.append(
                    (column, ValueWrapper(dialect.types.get_db_value(field, field.get_auto_now_value(now), None)))
                )
        if not is_insert and (version_field := meta.optimistic_lock_field) is not None:
            version_column = meta.fields_db_projection[version_field]
            if version_column not in written_columns:
                assignments.append((version_column, expression_context.table[version_column] + 1))
        if relation_row:
            relation_rows.append(relation_row)
        return assignments

    def add_tenant_value(self, values: dict[str, Any], is_insert: bool) -> None:
        """Puts the active tenant into an insert's values, or checks the tenant a branch writes.

        Raises:
            QueryError: The tenant is written from an expression under the active scope, is outside
                the scope, or an insert names none without a scope of one tenant.
        """
        model = self.target.model
        tenant_field = model._meta.tenant_field
        if tenant_field is None or self.target._visibility.all_tenants:
            return
        given, tenant, _source = Tenancy.get_given_tenant(model, values)
        if given and isinstance(tenant, (Expression, Term)):
            if Tenancy.get_scope(model) is not None:
                raise QueryError(
                    f"merge() writes {tenant_field!r} from an expression under the active tenant scope - give a plain "
                    "value, or call all_tenants() on the target"
                )
            return
        if is_insert:
            Tenancy.fill_create_values(model, values)
            return
        scope = Tenancy.get_scope(model)
        if given and scope is not None and not Tenancy.allows(model, scope, tenant):
            raise QueryError(f"merge() moves rows to {tenant_field}={tenant!r}, outside the active tenant scope")

    def raise_if_relation_unchecked(self, name: str, relation: ForeignKeyFieldInstance[Any], value: Any) -> None:
        """Rejects a relation to a tenant-scoped model written from an expression under the related
        model's active scope - the rows it points at can't be checked.

        Raises:
            QueryError: See above.
        """
        related_model: type[Model] = relation.related_model
        if (
            isinstance(value, (Expression, Term))
            and not self.target._visibility.all_tenants
            and related_model._meta.tenant_field
            and Tenancy.get_scope(related_model) is not None
        ):
            raise QueryError(
                f"merge() writes {name!r} from an expression under the active tenant scope of "
                f"{related_model.__name__} - the rows it points at can't be checked; call all_tenants() on the target"
            )
