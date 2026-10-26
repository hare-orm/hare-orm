from __future__ import annotations

from collections.abc import Awaitable, Generator, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import UnSupportedError
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.rules.constants import UPDATE_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.relation_key_values import RelationKeyValues
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.rules.tenant_write_rules import TenantWriteRules
from hare.models.write.update.expression_assignment import ExpressionAssignment
from hare.models.write.write_steps import WriteSteps
from hare.query.expressions import Expression, ExpressionContext, Value
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.expressions.value_references.write_value_reference import WriteValueReference
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.query_ordering import QueryOrdering
from hare.query.statements.constants import MOVED_KEY_EXPRESSION_MESSAGE
from hare.query.statements.write.matching_rows_query import MatchingRowsQuery
from hare.query.statements.write.returning.returned_rows import ReturnedRows
from hare.sql import Table
from hare.sql.functions.cast import Cast
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.time import Timezone

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.query.statements.write.returning.update_returning_query import UpdateReturningQuery


class UpdateQuery(MatchingRowsQuery):
    """``queryset.update()`` - fields set on the queryset's rows."""

    written_method_name: ClassVar[str] = "update"

    __slots__ = (
        "given_values",
        "update_kwargs",
        "_rules",
        "_method_name",
    )

    ordering_can_reference_annotation_alias: ClassVar[bool] = False

    #: The assigned values come last - a query of the queryset's calls binds them after the calls'.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *MatchingRowsQuery.plan_slots,
        ("_get_update_plan_description", PlanKeyForm.DESCRIBED),
    )

    def __init__(
        self,
        source: QuerySpecification[Any],
        update_kwargs: dict[str, Any],
        write_type: WriteType = WriteType.UPDATE,
        method_name: str | None = None,
    ) -> None:
        """
        Args:
            source: The queryset whose rows are updated, or a query made from one.
            update_kwargs: The values to set, by field name.
            write_type: ``WriteType.SOFT_DELETE`` for the soft delete or restore of the rows.
            method_name: The queryset method writing the rows, for a message - ``update`` when None.

        Raises:
            FieldError, QueryError: A value is given to a field the update can't set.
        """
        super().__init__(source)
        #: The queryset method writing the rows - delete() and restore() of a soft-delete model update them.
        self._method_name = method_name or self.written_method_name
        #: What the update writes - the fields refused, the stamps and the blind indexes.
        self._rules = RowWriteRules.get(
            self.model,
            write_type,
            update_kwargs,
            UPDATE_METHOD_NAME,
            stamps_named_fields=False,
            finds_rows_by_key=False,
        )
        #: The values as the caller gave them.
        self.given_values = update_kwargs
        #: The values written - the blind index of each encrypted field among them.
        self.update_kwargs = self._rules.add_blind_index_values(update_kwargs)

    def _get_written_method_name(self) -> str:
        return self._method_name

    def _orders_rows(self) -> bool:
        # An update orders its rows for its LIMIT alone.
        return self._limit is not None

    def _prepare_build(self) -> None:
        super()._prepare_build()
        # A literal an expression may write is checked by its field as a plain value is - on a plan
        # hit too, whose literals bind without their field.
        fields_map = self.model._meta.fields_map
        for key, value in self.update_kwargs.items():
            field_object = fields_map.get(key)
            if field_object is None or isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                continue
            value = ExpressionAssignment.get_assigned_value(value)
            if isinstance(value, Expression):
                ExpressionAssignment.raise_if_literal_refused(key, field_object, value, self.dialect)

    def _get_update_plan_description(self) -> PlanDescription | None:
        """Describes the assigned values - their structures, and the values bound.

        Returns:
            The description, None when a value can't be bound into a plan.
        """
        update_value_structures = self._get_update_value_structures()
        if update_value_structures is None:
            return None
        update_values, update_value_origins = self._get_update_values()
        return PlanDescription(update_value_structures, update_values, update_value_origins)

    def _get_call_signature_type_part(self) -> tuple[Any, list[Any]]:
        # The assigned values - their structure in the key, the values bound after the filters'.
        update_value_structures = self._get_update_value_structures()
        if update_value_structures is None or self._get_ctes_description().values:
            return None, []
        return update_value_structures, self._get_update_values()[0]

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        table = self.model._meta.basetable
        self._raise_if_rows_unwritable()
        self._make_filtered_update_query(table, value_wrapper_references)
        self._apply_update_values(table, value_wrapper_references)
        # The keys of a relation computed by the update, whose tenant is checked, are read by a SELECT
        # built from this query - run on its built query, never on a plan.
        return not self._get_tenant_checked_computed_relations()

    def _get_auto_now_fields(self) -> list[tuple[str, DatetimeField[Any] | TimeField[Any]]]:
        """The ``auto_now`` fields the update doesn't assign itself - each is set to the moment
        of the update.

        Returns:
            ``(field name, field)`` per field, in model field order.
        """
        return [(field_name, field_object) for field_name, field_object, _only in self._rules.stamped_fields]

    def _get_update_value_structures(self) -> tuple[Any, ...] | None:
        """The structure of every assigned value, for the statement's plan key: the field's name
        with the value's type, or with the structure of an expression.

        Returns:
            The structures, or None when a value can't be bound into a plan - a bare SQL term, an
            expression keeping no plan. An unknown field gives None too: the build raises for it.
        """
        fields_map = self.model._meta.fields_map
        structures: list[tuple[str, Any]] = []
        for key, value in self.update_kwargs.items():
            field_object = fields_map.get(key)
            if field_object is None:
                return None
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                if len(field_object.source_fields) > 1:
                    key_values = RelationKeyValues.get_key_values(self.model, key, value)
                    structures.append((key, tuple(type(key_value) for key_value in key_values)))
                    continue
                structures.append((key, type(value)))
                continue
            value = ExpressionAssignment.get_assigned_value(value)
            if isinstance(value, Expression):
                value_description = value.get_plan_description(PlanContext.EMPTY)
                if value_description is None:
                    return None
                structures.append((key, value_description.structure))
            else:
                structures.append((key, type(value)))
        return tuple(structures)

    def _get_update_values(self) -> tuple[list[Any], list[Any] | None]:
        """The values the statement assigns: each given value, then the moment every ``auto_now``
        field is set to.

        Returns:
            The values, and their origins while the statement records its plan (else None).

        Raises:
            ValidationError: A relation is assigned an instance of another model.
            QueryError: A relation is assigned an unsaved instance.
        """
        fields_map = self.model._meta.fields_map
        update_kwargs = self.update_kwargs
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        for key, value in update_kwargs.items():
            field_object = fields_map[key]
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)) and (
                len(field_object.source_fields) > 1
            ):
                key_values = RelationKeyValues.get_key_values(self.model, key, value)
                values.extend(key_values)
                if origins is not None:
                    origins += [
                        PlanOrigins.get_value_origin(update_kwargs, key, index) for index in range(len(key_values))
                    ]
                continue
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                # The check the build makes - it depends on the instance, not only on its type.
                values.append(RelationKeyValues.get_key_values(self.model, key, value)[0])
                if origins is not None:
                    origins.append(PlanOrigins.get_value_origin(update_kwargs, key))
                continue
            value = ExpressionAssignment.get_assigned_value(value)
            if isinstance(value, Expression):
                value_description: PlanDescription = value.get_plan_description(PlanContext.EMPTY)  # type: ignore[assignment]
                values.extend(value_description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(value_description, origins)
            else:
                values.append(value)
                if origins is not None:
                    origins.append(PlanOrigins.get_value_origin(update_kwargs, key))
        auto_now_fields = self._get_auto_now_fields()
        if auto_now_fields:
            now = Timezone.now()
            values.extend(field_object.get_auto_now_value(now) for _field_name, field_object in auto_now_fields)
            if origins is not None:
                origins += [
                    PlanOrigins.get_value_origin(self, "_auto_now_fields", index)
                    for index in range(len(auto_now_fields))
                ]
        return values, origins

    def _set_recorded(
        self,
        db_column: str,
        value: Any,
        field_object: Field[Any],
        value_wrapper_references: RecordedValueReferences | None,
        value_origin: tuple[Any, ...],
    ) -> None:
        """Adds ``db_column = value`` to the SET clause, recording where the value sits.

        Args:
            db_column: The assigned column.
            value: The value, converted for the write.
            field_object: The field a later statement's value is converted by.
            value_wrapper_references: The references being recorded, None when the statement keeps no
                plan. A value that isn't bound as a parameter (NULL, a list) records none.
            value_origin: The value's origin (``PlanOrigins``).
        """
        self.query = self.query.set(db_column, value)
        if value_wrapper_references is not None:
            assigned_term = self.query._updates[-1][1]
            value_wrapper_references.append(
                (
                    value_origin,
                    WriteValueReference(assigned_term, field_object)
                    if isinstance(assigned_term, ValueWrapper)
                    else None,
                )
            )

    def _make_filtered_update_query(
        self, table: Table, value_wrapper_references: RecordedValueReferences | None
    ) -> None:
        """Builds the UPDATE with this query's own filters - inline when they only read the base
        table, through a ``pk IN (SELECT ...)`` subquery when they join another one.

        Args:
            table: The base table.
            value_wrapper_references: Where the filters' value references are recorded, None when the
                statement keeps no plan.
        """
        self.query = self._get_base_query()
        if self._limit is not None:
            self.query._limit = self._limit_term = self.query._wrapper_class(self._limit)
            QueryOrdering.get_ordering(self, self.model, table, self._get_slice_orderings(), self._annotations)

        QueryConditions.get_filters(self, value_wrapper_references=value_wrapper_references)
        self._record_matching_rows_limit(value_wrapper_references)
        # An aggregate annotation in the filters needs its GROUP BY, or the HAVING aggregates over
        # the whole joined result.
        QueryGrouping.apply_auto_group_by(self)
        if self._filters_need_primary_key_subquery():
            # An UPDATE takes no JOINs - the rows are picked by a primary key subquery. The
            # annotation terms QueryConditions.get_filters() selected would become extra subquery columns.
            self.query._selects = []
            matching_rows_criterion = self._get_matching_rows_criterion(self.query)
            self.query = self._connection.query_class.update(table)
            self.query = self.query.where(matching_rows_criterion)

        else:
            update_query = self._connection.query_class.update(table)
            update_query._wheres = self.query._wheres
            update_query._limit = self.query._limit
            update_query._orderbys = self.query._orderbys
            self.query = update_query

    def hide_last_update_values(self) -> None:
        """Marks the values of the last assignment of the SET clause never shown (``QueryParameters``)."""
        for value_wrapper in self.query._updates[-1][1].find_(ValueWrapper):
            value_wrapper.sensitive = True

    def _set_composite_relation_keys(
        self,
        key: str,
        value: Any,
        field_object: ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any],
        value_wrapper_references: RecordedValueReferences | None,
    ) -> None:
        """Sets every key column of a relation to a composite key.

        Args:
            key: The relation's name.
            value: The related object, a tuple of the key's values or None.
            field_object: The relation.
            value_wrapper_references: Where each assigned value's reference is recorded - None when
                the statement keeps no plan.

        Raises:
            QueryError: The value is neither a related object, a tuple of the key's length nor None.
        """
        for index, (source_field_name, key_value) in enumerate(
            zip(field_object.source_fields, RelationKeyValues.get_key_values(self.model, key, value), strict=True)
        ):
            source_field_object = self.model._meta.fields_map[source_field_name]
            self._set_recorded(
                source_field_object.source_field or source_field_name,
                self.dialect.types.get_db_value(source_field_object, key_value, None),
                source_field_object,
                value_wrapper_references,
                PlanOrigins.get_value_origin(self.update_kwargs, key, index),
            )
            if source_field_object.sensitive:
                self.hide_last_update_values()

    def _get_relation_key_value(
        self, key: str, value: Any, field_object: ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any]
    ) -> tuple[str, Any, Field[Any] | None]:
        """The key column a relation is written to, and the related object's key written there.

        Args:
            key: The relation's name.
            value: The related object, None to clear the relation.
            field_object: The relation.

        Returns:
            The column, the value, and the key column's field.

        Raises:
            ValidationError, QueryError: The value isn't a saved object of the related model.
        """
        foreign_key_field: str = field_object.source_field  # type: ignore[assignment]
        value_field_object = self.model._meta.fields_map[foreign_key_field]
        db_value = self.dialect.types.get_db_value(
            value_field_object, RelationKeyValues.get_key_values(self.model, key, value)[0], None
        )
        return cast("str", value_field_object.source_field), db_value, value_field_object

    def _get_assigned_column_value(
        self,
        key: str,
        value: Any,
        field_object: Field[Any],
        table: Table,
        value_wrapper_references: RecordedValueReferences | None,
    ) -> tuple[str, Any, Field[Any] | None]:
        """The column a field other than a relation is written to, and the value written - an
        expression as its term.

        Args:
            key: The field's name.
            value: The value given.
            field_object: The field.
            table: The base table.
            value_wrapper_references: Where each assigned value's reference is recorded - None when
                the statement keeps no plan.

        Returns:
            The column, the value, and the field the value's reference is recorded by - None for an
            expression, which recorded the references of its own literals.

        """
        db_field = self.model._meta.fields_db_projection[key]
        value = ExpressionAssignment.get_assigned_value(value)
        if not isinstance(value, Expression):
            return db_field, self.dialect.types.get_db_value(field_object, value, None), field_object
        term = ExpressionAssignment.get_term(
            key,
            field_object,
            value,
            ExpressionContext(
                model=self.model,
                dialect=self.dialect,
                connection=self._connection,
                table=table,
                annotations=self._annotations,
                value_wrapper_references=value_wrapper_references,
            ),
        )
        return db_field, term, None

    def _set_automatic_values(self, table: Table, value_wrapper_references: RecordedValueReferences | None) -> None:
        """Adds the values every update writes whatever it is given: the bump of
        ``Meta.optimistic_lock_field`` and the ``auto_now`` stamps.

        Args:
            table: The base table.
            value_wrapper_references: Where each assigned value's reference is recorded - None when
                the statement keeps no plan.
        """
        if self.update_kwargs and (optimistic_lock_field := self._rules.version_field_name):
            # The optimistic lock field is bumped in the SET clause (version = version + 1):
            # update() has read no row whose version it could compare.
            version_db_column = self.model._meta.fields_db_projection[optimistic_lock_field]
            self.query = self.query.set(version_db_column, table[version_db_column] + 1)

        # auto_now fields are always written - every row gets the same moment.
        auto_now_fields = self._get_auto_now_fields()
        if auto_now_fields:
            now = Timezone.now()
            for index, (field_name, auto_now_field_object) in enumerate(auto_now_fields):
                self._set_recorded(
                    self.model._meta.fields_db_projection[field_name],
                    self.dialect.types.get_db_value(
                        auto_now_field_object, auto_now_field_object.get_auto_now_value(now), None
                    ),
                    auto_now_field_object,
                    value_wrapper_references,
                    PlanOrigins.get_value_origin(self, "_auto_now_fields", index),
                )

    def _apply_update_values(self, table: Table, value_wrapper_references: RecordedValueReferences | None) -> None:
        """Adds the SET clause - the given values, plus the automatic version/auto_now bumps -
        and the WITH clause to the built UPDATE.

        Args:
            table: The base table.
            value_wrapper_references: Where each assigned value's reference is recorded, with the value's
                origin - None when the statement keeps no plan.

        Raises:
            FieldError: A value is an aggregate.
            QueryError: A value references a related model's field or a window function.
            UnSupportedError: A field is a key column the database doesn't update in place.
        """
        table_options = self.model._meta.get_table_options(self.dialect)
        if table_options is not None:
            table_options.raise_if_fixed_fields_written(self.model, self.update_kwargs)
        fields_map = self.model._meta.fields_map
        for key, value in self.update_kwargs.items():
            field_object = fields_map[key]
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                if len(field_object.source_fields) > 1:
                    self._set_composite_relation_keys(key, value, field_object, value_wrapper_references)
                    continue
                db_field, value, value_field_object = self._get_relation_key_value(key, value, field_object)
            else:
                db_field, value, value_field_object = self._get_assigned_column_value(
                    key, value, field_object, table, value_wrapper_references
                )

            if value_field_object is None:
                self.query = self.query.set(db_field, value)
            else:
                self._set_recorded(
                    db_field,
                    value,
                    value_field_object,
                    value_wrapper_references,
                    PlanOrigins.get_value_origin(self.update_kwargs, key),
                )
            # The value written into a sensitive field is never shown (QueryParameters) - nor a relation's
            # key column, sensitive as the key it points at is.
            if field_object.sensitive or (value_field_object is not None and value_field_object.sensitive):
                self.hide_last_update_values()

        self._set_automatic_values(table, value_wrapper_references)

        capture_needs = self.model._meta.change_capture_needs
        if (
            capture_needs is not None
            and capture_needs.captures(RowOperation.UPDATE)
            and self._connection.features.supports_returning
        ):
            # The rows' keys and captured fields for Meta.change_capture - as they were too, where the
            # database returns them.
            self.query = self.query.returning(*capture_needs.columns)
            if capture_needs.reads_before and self._connection.features.supports_returning_old_new:
                self.query = self.query.returning(*ChangeCapturing.get_old_value_terms(self.model, capture_needs))

        # Last - the joined-subquery branch replaces self.query with a new builder, which would drop a WITH
        # clause applied earlier; the select and aggregate queries apply theirs last too.
        QueryCtes.apply_with_ctes(self, value_wrapper_references=value_wrapper_references)

    def returning(self, *field_names: str, old: Sequence[str] = ()) -> UpdateReturningQuery:
        """The update returning each row it wrote - ``UPDATE ... RETURNING``: a dict of
        ``field_names`` per row, or the model instances when none is named. With ``old``, each dict
        also holds those fields as they were before the update, under ``"old"``.

        Args:
            field_names: The fields - a concrete field, a forward relation (its key) or ``pk``.
            old: The fields returned as they were before the update too - needs ``field_names``.

        Returns:
            The update, its result the list of rows.

        Raises:
            FieldError: A name isn't a field written in the model's table, or is given twice.
            UnSupportedError: When run - the database has no ``RETURNING``, or no ``RETURNING``
                of the old values (``Features.supports_returning_old_new``).
        """
        from hare.query.statements.write.returning.update_returning_query import UpdateReturningQuery

        return UpdateReturningQuery(
            self, self.given_values, ReturnedRows(self.model, field_names, old), self._rules.write_type
        )

    def __await__(self) -> Generator[Any, None, int | None]:
        if self._is_none:
            return self._execute_none().__await__()
        query = self._get_execution_query(True)
        capture_needs = query.model._meta.change_capture_needs
        if capture_needs is not None and capture_needs.captures(RowOperation.UPDATE):
            return WriteSteps.run_whole(query._connection, query._execute_captured(capture_needs)).__await__()
        query._make_query_to_run()
        return WriteSteps.get_whole_run(query._connection, query._report_update(query._run_statement())).__await__()

    def _get_query_on(self, connection: DatabaseClient) -> UpdateQuery:
        """This update, run on another connection - a transaction opened for it.

        Args:
            connection: The connection.

        Returns:
            The update.
        """
        query = UpdateQuery(self, self.given_values, self._rules.write_type, self._method_name)
        query._apply_connection(connection)
        query._connection_explicitly_chosen = True
        return query

    async def _execute_captured(self, capture_needs: CaptureNeeds) -> int | None:
        """Runs the update of a model with ``Meta.change_capture`` and captures the rows it wrote, in
        one transaction.

        Args:
            capture_needs: The model's needs.

        Returns:
            How many rows it updated.
        """
        captured = self._get_captured_write(capture_needs, self._connection)
        async with captured.transaction() as connection:
            query = self if connection is self._connection else self._get_query_on(connection)
            if connection.features.supports_returning:
                count, raw_rows = await query._run_captured(captured)
                pks = [change_pk for change_pk, _tenant in self._get_returned_keys(capture_needs, raw_rows)]
            else:
                count, pks = await query._run_captured_by_reading(captured, query._run_built_statement)
        # A count the database doesn't report - the rows read are the changed ones.
        if count or (count is None and pks):
            await WriteSteps.report(
                self._connection, self.model, RowOperation.UPDATE, pks=pks, fields=self._rules.changed_field_names
            )
        return count

    def _get_captured_write(self, capture_needs: CaptureNeeds, connection: DatabaseClient) -> CapturedWrite:
        """The capture of the update - the fields its caller named reported as changed.

        Args:
            capture_needs: The model's needs.
            connection: The connection of the update.

        Returns:
            The capture.
        """
        return CapturedWrite(
            self.model, capture_needs, connection, RowOperation.UPDATE, changed=self._rules.changed_field_names
        )

    def _get_returned_keys(self, capture_needs: CaptureNeeds, raw_rows: Sequence[Any]) -> list[tuple[Any, Any]]:
        """The primary keys and tenants of the rows the update returned.

        Args:
            capture_needs: The model's needs.
            raw_rows: The rows.

        Returns:
            ``(primary key, tenant)`` per row.
        """
        types = self._connection.dialect.types
        return [ChangeCapturing.get_row_key(self.model, capture_needs, types, dict(row)) for row in raw_rows]

    async def _run_captured(self, captured: CapturedWrite) -> tuple[int | None, Sequence[Any]]:
        """Runs the update - on a transaction - returning its rows, and gives their changes to the
        model's sink. The rows as they were come from ``RETURNING OLD``, else from reading the matched
        rows first, locked, in one query - the update then writes those rows alone, so a row committed
        by another transaction in between is left as it is.

        Args:
            captured: The capture of the update, on this query's connection.

        Returns:
            The number of updated rows and the returned rows.
        """
        capture_needs = captured.needs
        returns_old = capture_needs.reads_before and self._connection.features.supports_returning_old_new
        before_by_pk: dict[Any, Any] | None = None
        if capture_needs.reads_before and not returns_old:
            # Local import: the queryset package imports the query statements.
            from hare.query.queryset.selection.statement_selection import StatementSelection

            before_by_pk = await captured.read_before(
                StatementSelection.get_primary_key_values_query(self._matching_queryset())
            )
            if not before_by_pk:
                return 0, []
            moved_keys = self._get_moved_keys(list(before_by_pk))
            if moved_keys:
                # The returned rows hold the keys the update gave them.
                before_by_pk = {moved_keys[pk]: values for pk, values in before_by_pk.items()}
                restricted_keys = list(moved_keys)
            else:
                restricted_keys = list(before_by_pk)
            self._filter_call_counter += 1
            generation = self._filter_call_counter
            PendingFilterCalls.add_filter_conditions(
                self,
                False,
                PendingFilterCalls.get_filter_kwarg_conditions(self, {"pk__in": restricted_keys}, generation),
                generation,
            )
        self._make_query_to_run()
        count, raw_rows = await self._run_statement(returns_rows=True)
        await captured.capture_returned(raw_rows, returns_old=returns_old, before_by_pk=before_by_pk)
        return count, raw_rows

    def _get_moved_keys(self, keys: list[Any]) -> dict[Any, Any]:
        """The primary key each row of ``keys`` has after the update - an update setting a column of a composite
        primary key moves its rows.

        Args:
            keys: The keys of the rows updated.

        Returns:
            The key after the update by the key before it; empty for an update setting no key column.

        Raises:
            UnSupportedError: A key column is set from an expression - which row went to which key is unknown.
        """
        model = self.model
        meta = model._meta
        if not meta.has_composite_primary_key:
            return {}
        key_names = meta.primary_key_attribute_names
        new_values: dict[str, Any] = {}
        for name, value in self.given_values.items():
            stored_names = RowWriteRules.get_stored_field_names(model, [name], UPDATE_METHOD_NAME)
            if not any(stored_name in key_names for stored_name in stored_names):
                continue
            if isinstance(value, (Expression, Term)):
                raise UnSupportedError(MOVED_KEY_EXPRESSION_MESSAGE.format(model=model.__name__, field=name))
            relation = meta.fields_map.get(name)
            stored_values = (
                RelationKeyValues.get_key_values(model, name, value)
                if isinstance(relation, (ForeignKeyFieldInstance, OneToOneFieldInstance))
                else (value,)
            )
            for stored_name, stored_value in zip(stored_names, stored_values, strict=True):
                if stored_name in key_names:
                    new_values[stored_name] = meta.fields_map[stored_name].to_python(stored_value)
        if not new_values:
            return {}
        return {
            key: tuple(new_values.get(name, part) for name, part in zip(key_names, key, strict=True)) for key in keys
        }

    async def _run_built_statement(self) -> int | None:
        """Builds and runs the update.

        Returns:
            How many rows it updated.
        """
        self._make_query_to_run()
        return (await self._run_statement())[0]

    async def _report_update(self, update: Awaitable[tuple[int | None, Sequence[Any]]]) -> int | None:
        """Runs the update and reports the rows it changed (``ChangeEvents``).

        Args:
            update: The update's statement (``_run_statement()``).

        Returns:
            How many rows it updated.
        """
        count = (await update)[0]
        # _report_updated_rows() written out - an update runs no coroutine more than it needs. A count the
        # database doesn't report may be of changed rows.
        if (count is None or count) and ChangeEvents.is_observed(self.model):
            await WriteSteps.report(
                self._connection, self.model, RowOperation.UPDATE, fields=self._rules.changed_field_names
            )
        return count

    async def _report_updated_rows(self, count: int | None) -> None:
        """Reports the rows an update changed (``ChangeEvents``).

        Args:
            count: How many rows it updated.
        """
        if (count is None or count) and ChangeEvents.is_observed(self.model):
            await WriteSteps.report(
                self._connection, self.model, RowOperation.UPDATE, fields=self._rules.changed_field_names
            )

    async def _execute_none(self) -> int:
        return 0

    async def _execute(self) -> int | None:
        return (await self._run_statement())[0]

    async def _run_statement(self, *, returns_rows: bool = False) -> tuple[int | None, Sequence[Any]]:
        """Checks the tenants the update writes and runs the built ``UPDATE``.

        Args:
            returns_rows: Whether the statement returns rows of its own (``returning()``).

        Returns:
            The number of updated rows and the returned rows.
        """
        if self.model._meta.tenant_field:
            TenantWriteRules.raise_if_rows_leave_scope(
                self.model, self.update_kwargs, self._visibility.all_tenants, UPDATE_METHOD_NAME, requires_scope=True
            )
        await self._check_relation_tenants()
        parameterized_sql = self._get_parameterized_sql()
        if not parameterized_sql[0]:
            # Nothing to set builds no statement - no row is updated, on every driver.
            return 0, []
        return await self._connection.execute(*parameterized_sql, returns_rows=returns_rows)

    async def _check_relation_tenants(self) -> None:
        """Rejects an update pointing a relation at a row its model's tenant scope doesn't show.

        Raises:
            QueryError: A relation value names another tenant's row.
        """
        if self._visibility.all_tenants or not Tenancy.has_tenant_scoped_relations(self.model):
            return

        meta = self.model._meta
        await self._check_computed_relation_tenants()
        row: dict[str, Any] = {}
        for key, value in self.update_kwargs.items():
            value = self._get_constant_value(value)
            if isinstance(value, (Expression, Term)):
                continue
            field_object = meta.fields_map.get(key)
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                key_values = RelationKeyValues.get_key_values(self.model, key, value)
                row.update(zip(field_object.source_fields, key_values, strict=True))
            else:
                row[key] = value
        await Tenancy.check_relation_targets(self.model, [row], self._connection, set(self.update_kwargs))

    @staticmethod
    def _get_constant_value(value: Any) -> Any:
        """The value a ``Value(...)`` stands for - a constant the update writes as given - else the
        value itself.

        Args:
            value: An update value.

        Returns:
            The value.
        """
        return value.value if type(value) is Value else value

    def _get_computed_relations(self) -> list[RelationalField[Any]]:
        """The forward relations whose new key the update's values don't give alone - set from an
        expression, or one column of a key of several set while the others keep the row's.

        Returns:
            The relations.
        """
        meta = self.model._meta
        relations: list[RelationalField[Any]] = []
        for relation_name in sorted(meta.foreign_key_fields | meta.one_to_one_fields):
            relation = cast("RelationalField[Any]", meta.fields_map[relation_name])
            set_names = [name for name in (relation_name, *relation.source_fields) if name in self.update_kwargs]
            if not set_names:
                continue
            sets_expression = any(
                isinstance(self._get_constant_value(self.update_kwargs[name]), (Expression, Term))
                for name in set_names
            )
            sets_part_of_key = relation_name not in set_names and len(set_names) < len(relation.source_fields)
            if sets_expression or sets_part_of_key:
                relations.append(relation)
        return relations

    def _get_tenant_checked_computed_relations(self) -> list[RelationalField[Any]]:
        """The relations of ``_get_computed_relations()`` to a tenant-scoped model."""
        return [relation for relation in self._get_computed_relations() if relation.related_model._meta.tenant_field]

    async def _check_computed_relation_tenants(self) -> None:
        """Rejects an update leaving a relation pointing at a row its model's tenant scope doesn't
        show, where the new key is computed - read by a ``SELECT`` of what the update writes into the
        key columns, for the rows it matches, before it runs.

        Raises:
            QueryError: A row's new key names another tenant's row.
        """
        relations = self._get_tenant_checked_computed_relations()
        if not relations:
            return
        meta = self.model._meta
        table = meta.basetable
        assigned_terms = {field.name: term for field, term in self.query._updates}
        key_field_names = [source_field for relation in relations for source_field in relation.source_fields]
        dialect = self._connection.dialect
        selected_terms = []
        for key_field_name in key_field_names:
            column = meta.fields_db_projection[key_field_name]
            assigned_term = assigned_terms.get(column)
            if assigned_term is None:
                selected_terms.append(table[column].as_(key_field_name))
                continue
            # A value bound alone in the select list has no type of its own - the column's is given.
            column_type = meta.fields_map[key_field_name].get_column_type(dialect)
            selected_terms.append(Cast(assigned_term, column_type).as_(key_field_name))
        values_query = self._connection.query_class.from_(table).select(*selected_terms)
        if self.query._wheres is not None:
            values_query = values_query.where(self.query._wheres)
        _row_count, raw_rows = await self._connection.execute(*values_query.get_parameterized_sql(), returns_rows=True)
        types = self._connection.dialect.types
        rows = []
        for raw_row in raw_rows:
            raw_values = (
                dict(raw_row) if hasattr(raw_row, "keys") else dict(zip(key_field_names, raw_row, strict=True))
            )
            rows.append(
                {
                    name: None if value is None else types.get_python_value(meta.fields_map[name], value)
                    for name, value in raw_values.items()
                }
            )
        await Tenancy.check_relation_targets(
            self.model, rows, self._connection, {relation.model_field_name for relation in relations}
        )
