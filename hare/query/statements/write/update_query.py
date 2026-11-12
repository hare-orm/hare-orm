from __future__ import annotations

from collections.abc import Awaitable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import UnSupportedError
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy.update_relation_tenants import UpdateRelationTenants
from hare.models.write.rules.constants import UPDATE_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.relation_key_values import RelationKeyValues
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.rules.tenant_write_rules import TenantWriteRules
from hare.models.write.update.expression_assignment import ExpressionAssignment
from hare.models.write.write_steps import WriteSteps
from hare.query.execution.prepared_write import PreparedWrite
from hare.query.expressions import Expression, ExpressionContext
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.expressions.value_references.write_value_reference import WriteValueReference
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plan_parts import PlanParts
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.declarations import CallPlanHit
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.constants import MOVED_KEY_EXPRESSION_MESSAGE
from hare.query.statements.write.matching_rows_query import MatchingRowsQuery
from hare.query.statements.write.returning.returned_rows import ReturnedRows
from hare.sql import Table
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.time import Timezone

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.query.queryset.queryset import QuerySet
    from hare.query.scopes.row_visibility import RowVisibility
    from hare.query.statements.write.returning.update_returning_query import UpdateReturningQuery


class UpdateQuery(MatchingRowsQuery):
    """``queryset.update()`` - fields set on the queryset's rows."""

    written_method_name: ClassVar[str] = "update"

    __slots__ = (
        "given_values",
        "update_kwargs",
        "_rules",
        "_method_name",
        "_prepared_write",
        "_assignment_fields",
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
        fields = self.model._meta.fields_map
        self._assignment_fields = tuple(
            (
                name,
                fields[name],
                tuple(fields[source] for source in fields[name].source_fields)
                if isinstance(fields[name], (ForeignKeyFieldInstance, OneToOneFieldInstance))
                else (),
            )
            for name in self.update_kwargs
        )
        self._prepared_write: PreparedWrite | None = None

    def _prepare_write_values(self) -> PreparedWrite:
        """Bind direct assignments and automatic stamps once for the execution this query is made for
        - see ``_make_prepared_write()``."""
        if self._prepared_write is None:
            self._prepared_write = self._make_prepared_write(self.dialect)
        return self._prepared_write

    def _make_prepared_write(self, dialect: Dialect) -> PreparedWrite:
        """Bind direct assignments and automatic stamps for one execution - this query is only read.

        Expressions keep their own parameter descriptions. Relation assignments are
        checked and expanded before compilation, so both plan hits and full builds
        consume the same column values, including one shared automatic timestamp.

        Args:
            dialect: The dialect of the execution's connection.
        """
        assignments: list[tuple[str, int, Field[Any], Any]] = []
        for name, field, source_fields in self._assignment_fields:
            value = self.update_kwargs[name]
            if source_fields:
                values = RelationKeyValues.get_key_values(self.model, name, value)
                assignments.extend(
                    (name, index, source_field, item)
                    for index, (source_field, item) in enumerate(zip(source_fields, values, strict=True))
                )
            else:
                value = ExpressionAssignment.get_assigned_value(value)
                if not isinstance(value, Expression):
                    assignments.append((name, 0, field, value))
        automatic_fields = self._get_auto_now_fields()
        now = Timezone.now() if automatic_fields else None
        return PreparedWrite(
            dialect,
            assignments,
            ((name, field, field.get_auto_now_value(now)) for name, field in automatic_fields),
        )

    def _get_written_method_name(self) -> str:
        return self._method_name

    def _orders_rows(self) -> bool:
        # An update orders its rows for its LIMIT alone.
        return self._limit is not None

    def _prepare_build(self) -> None:
        super()._prepare_build()
        self._prepare_write_values()
        self._check_expression_literals(self.dialect)

    def _check_expression_literals(self, dialect: Dialect) -> None:
        """Checks each literal an expression may write by its field, as a plain value is - on a run on a
        plan too, whose literals bind without their field. This query is only read.

        Args:
            dialect: The dialect of the execution's connection.
        """
        update_kwargs = self.update_kwargs
        for key, field_object, source_fields in self._assignment_fields:
            if source_fields:
                continue
            value = ExpressionAssignment.get_assigned_value(update_kwargs[key])
            if isinstance(value, Expression):
                ExpressionAssignment.raise_if_literal_refused(key, field_object, value, dialect)

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

    def _get_call_signature_type_part(self, prepared: PreparedWrite | None = None) -> tuple[Any, Sequence[Any]]:
        """The assigned values - their structure in the key, the values bound after the filters'.

        Args:
            prepared: The values of the execution run on a plan of the calls - this query's own
                (``_prepare_write_values()``) when None.
        """
        update_value_structures = self._get_update_value_structures()
        if update_value_structures is None or self._options.with_ctes:
            return None, ()
        return update_value_structures, self._get_update_values(prepared)[0]

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        table = self.model._meta.basetable
        self._raise_if_rows_unwritable()
        self._make_filtered_update_query(table, value_wrapper_references)
        self._apply_update_values(table, value_wrapper_references)
        # The keys of a relation computed by the update, whose tenant is checked, are read by a SELECT
        # built from this query - run on its built query, never on a plan.
        return not UpdateRelationTenants.get_tenant_checked_computed_relations(self)

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
        update_kwargs = self.update_kwargs
        structures: list[tuple[str, Any]] = []
        for key, _field_object, source_fields in self._assignment_fields:
            value = update_kwargs[key]
            if source_fields:
                if len(source_fields) > 1:
                    key_values = RelationKeyValues.get_key_values(self.model, key, value)
                    structures.append((key, tuple(type(key_value) for key_value in key_values)))
                else:
                    structures.append((key, type(value)))
                continue
            value = ExpressionAssignment.get_assigned_value(value)
            if isinstance(value, Expression):
                value_description = value.get_plan_description(PlanContext(model=self.model))
                if value_description is None:
                    return None
                structures.append((key, value_description.structure))
            else:
                structures.append((key, type(value)))
        return tuple(structures)

    def _get_update_values(self, prepared: PreparedWrite | None = None) -> tuple[list[Any], list[Any] | None]:
        """The values the statement assigns: each given value, then the moment every ``auto_now``
        field is set to.

        Args:
            prepared: The execution's values - this query's own (``_prepare_write_values()``) when None.

        Returns:
            The values, and their origins while the statement records its plan (else None).

        Raises:
            ValidationError: A relation is assigned an instance of another model.
            QueryError: A relation is assigned an unsaved instance.
        """
        update_kwargs = self.update_kwargs
        if prepared is None:
            prepared = self._prepare_write_values()
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        for key, _field_object, source_fields in self._assignment_fields:
            if len(source_fields) > 1:
                key_values = [prepared.values[key, index] for index in range(len(source_fields))]
                values.extend(key_values)
                if origins is not None:
                    origins += [
                        PlanOrigins.get_value_origin(update_kwargs, key, index) for index in range(len(key_values))
                    ]
                continue
            if source_fields:
                values.append(prepared.values[key, 0])
                if origins is not None:
                    origins.append(PlanOrigins.get_value_origin(update_kwargs, key))
                continue
            value = ExpressionAssignment.get_assigned_value(update_kwargs[key])
            if isinstance(value, Expression):
                value_description: PlanDescription = value.get_plan_description(PlanContext(model=self.model))  # type: ignore[assignment]
                values.extend(value_description.values)
                if origins is not None:
                    origins = PlanParts.get_origins(value_description, origins)
            else:
                values.append(prepared.values[key, 0])
                if origins is not None:
                    origins.append(PlanOrigins.get_value_origin(update_kwargs, key))
        if prepared.automatic_values:
            values.extend(value for _field_name, value in prepared.automatic_values)
            if origins is not None:
                origins += [
                    PlanOrigins.get_value_origin(self, "_auto_now_fields", index)
                    for index in range(len(prepared.automatic_values))
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
        self._build_matching_rows(value_wrapper_references)
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
        field_object: ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any],
        value_wrapper_references: RecordedValueReferences | None,
    ) -> None:
        """Sets every key column of a relation to a composite key.

        Args:
            key: The relation's name.
            field_object: The relation.
            value_wrapper_references: Where each assigned value's reference is recorded - None when
                the statement keeps no plan.

        Raises:
            QueryError: The value is neither a related object, a tuple of the key's length nor None.
        """
        prepared = self._prepare_write_values()
        for index, source_field_name in enumerate(field_object.source_fields):
            source_field_object = self.model._meta.fields_map[source_field_name]
            self._set_recorded(
                source_field_object.source_field or source_field_name,
                prepared.values[key, index].database_value,
                source_field_object,
                value_wrapper_references,
                PlanOrigins.get_value_origin(self.update_kwargs, key, index),
            )
            if source_field_object.sensitive:
                self.hide_last_update_values()

    def _get_relation_key_value(
        self, key: str, field_object: ForeignKeyFieldInstance[Any] | OneToOneFieldInstance[Any]
    ) -> tuple[str, Any, Field[Any] | None]:
        """The key column a relation is written to, and the related object's key written there.

        Args:
            key: The relation's name.
            field_object: The relation.

        Returns:
            The column, the value, and the key column's field.

        Raises:
            ValidationError, QueryError: The value isn't a saved object of the related model.
        """
        foreign_key_field: str = field_object.source_field  # type: ignore[assignment]
        value_field_object = self.model._meta.fields_map[foreign_key_field]
        db_value = self._prepare_write_values().values[key, 0].database_value
        return value_field_object.source_field, db_value, value_field_object

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
            return db_field, self._prepare_write_values().values[key, 0].database_value, field_object
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
        for index, (field_name, value) in enumerate(self._prepare_write_values().automatic_values):
            self._set_recorded(
                self.model._meta.fields_db_projection[field_name],
                value.database_value,
                value.field,
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
                    self._set_composite_relation_keys(key, field_object, value_wrapper_references)
                    continue
                db_field, value, value_field_object = self._get_relation_key_value(key, field_object)
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

    async def _execute_bound(self) -> int | None | list[Any]:
        if self._is_none:
            return await self._execute_none()
        connection = self.get_connection(for_write=True)
        prepared = self._make_prepared_write(connection.dialect)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is not None and capture_needs.captures(RowOperation.UPDATE):
            return await WriteSteps.get_whole_run(
                connection, self._execute_captured(capture_needs, connection, prepared)
            )
        return await WriteSteps.get_whole_run(
            connection, self._report_update(self._run_on(connection, prepared), connection)
        )

    async def _run_on(
        self,
        connection: DatabaseClient,
        prepared: PreparedWrite,
        source: QuerySet[Any] | None = None,
        *,
        returns_rows: bool = False,
    ) -> tuple[int | None, Sequence[Any]]:
        """Find or build this update's statement on the execution connection, without rebinding the declaration."""
        statement = self._get_statement_on(
            connection, source, type_part=self._get_call_signature_type_part(prepared), prepared=prepared
        )
        if isinstance(statement, CallPlanHit):
            self._check_expression_literals(connection.dialect)
            return await self._run_statement_on(
                connection, statement.sql, statement.parameters, statement.visibility, returns_rows=returns_rows
            )
        query, command = statement
        return await query._run_statement_on(connection, *command, query._visibility, returns_rows=returns_rows)

    async def _execute_captured(
        self, capture_needs: CaptureNeeds, execution_connection: DatabaseClient, prepared: PreparedWrite
    ) -> int | None:
        """Runs the update of a model with ``Meta.change_capture`` and captures the rows it wrote, in
        one transaction.

        Args:
            capture_needs: The model's needs.

        Returns:
            How many rows it updated.
        """
        captured = self._get_captured_write(capture_needs, execution_connection)
        async with captured.transaction() as connection:
            if connection.features.supports_returning:
                count, raw_rows = await self._run_captured(captured, prepared)
                pks = [
                    change_pk for change_pk, _tenant in self._get_returned_keys(capture_needs, raw_rows, connection)
                ]
            else:

                async def run_statement(source: QuerySet[Any]) -> int | None:
                    return (await self._run_on(connection, prepared, source))[0]

                count, pks = await self._run_captured_by_reading(
                    captured,
                    run_statement,
                    get_moved_keys=lambda keys: self._get_moved_keys(keys, connection, prepared),
                )
        # A count the database doesn't report - the rows read are the changed ones.
        if count or (count is None and pks):
            await WriteSteps.report(
                execution_connection, self.model, RowOperation.UPDATE, pks=pks, fields=self._rules.changed_field_names
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

    def _get_returned_keys(
        self, capture_needs: CaptureNeeds, raw_rows: Sequence[Any], connection: DatabaseClient
    ) -> list[tuple[Any, Any]]:
        """The primary keys and tenants of the rows the update returned.

        Args:
            capture_needs: The model's needs.
            raw_rows: The rows.

        Returns:
            ``(primary key, tenant)`` per row.
        """
        types = connection.dialect.types
        names = (
            *self.model._meta.primary_key_attribute_names,
            *((capture_needs.tenant_field_name,) if capture_needs.tenant_field_name else ()),
        )
        layout = ChangeCapturing.get_row_layout(self.model, names, types)
        return [
            ChangeCapturing.get_row_key(self.model, capture_needs, types, dict(row), layout=layout) for row in raw_rows
        ]

    async def _run_captured(
        self, captured: CapturedWrite, prepared: PreparedWrite
    ) -> tuple[int | None, Sequence[Any]]:
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
        connection = captured.connection
        returns_old = capture_needs.reads_before and connection.features.supports_returning_old_new
        before_by_pk: dict[Any, Any] | None = None
        source = None
        if capture_needs.reads_before and not returns_old:
            # Local import: the queryset package imports the query statements.
            from hare.query.queryset.selection.statement_selection import StatementSelection

            source = self._matching_queryset(connection)
            before_by_pk = await captured.read_before(StatementSelection.get_primary_key_values_query(source))
            if not before_by_pk:
                return 0, []
            restricted_keys = list(before_by_pk)
            moved_keys = self._get_moved_keys(restricted_keys, connection, prepared)
            if moved_keys:
                # The returned rows hold the keys the update gave them.
                before_by_pk = {moved_keys[pk]: values for pk, values in before_by_pk.items()}
            source = self._filter_matching_rows(source, pk__in=restricted_keys)
        count, raw_rows = await self._run_on(connection, prepared, source, returns_rows=True)
        await captured.capture_returned(raw_rows, returns_old=returns_old, before_by_pk=before_by_pk)
        return count, raw_rows

    def _get_moved_keys(self, keys: list[Any], connection: DatabaseClient, prepared: PreparedWrite) -> dict[Any, Any]:
        """The primary key each row of ``keys`` has after the update - an update setting a column of a composite
        primary key moves its rows.

        Args:
            keys: The keys of the rows updated.
            connection: The execution connection whose readers decode the written keys.
            prepared: The very assignments passed to the UPDATE; never convert them again.

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
        types = connection.dialect.types
        for name, field, source_fields in self._assignment_fields:
            stored_fields = source_fields or (field,)
            if not any(column.model_field_name in key_names for column in stored_fields):
                continue
            if isinstance(self.update_kwargs[name], (Expression, Term)):
                raise UnSupportedError(MOVED_KEY_EXPRESSION_MESSAGE.format(model=model.__name__, field=name))
            for index, column in enumerate(stored_fields):
                if column.model_field_name in key_names:
                    value = prepared.values[name, index]
                    new_values[column.model_field_name] = types.get_python_value(column, value.database_value)
        for name, value in prepared.automatic_values:
            if name in key_names:
                new_values[name] = types.get_python_value(value.field, value.database_value)
        if not new_values:
            return {}
        return {
            key: tuple(new_values.get(name, part) for name, part in zip(key_names, key, strict=True)) for key in keys
        }

    async def _report_update(
        self, update: Awaitable[tuple[int | None, Sequence[Any]]], connection: DatabaseClient
    ) -> int | None:
        """Runs the update and reports the rows it changed (``ChangeEvents``).

        Args:
            update: The update's statement.
            connection: The execution's connection.

        Returns:
            How many rows it updated.
        """
        count = (await update)[0]
        await self._report_updated_rows(count, connection)
        return count

    async def _report_updated_rows(self, count: int | None, connection: DatabaseClient) -> None:
        """Reports the rows an update changed (``ChangeEvents``).

        Args:
            count: How many rows it updated.
            connection: The execution's connection.
        """
        if (count is None or count) and ChangeEvents.is_observed(self.model):
            await WriteSteps.report(
                connection,
                self.model,
                RowOperation.UPDATE,
                fields=self._rules.changed_field_names,
            )

    async def _execute_none(self) -> int:
        return 0

    async def _run_statement_on(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        visibility: RowVisibility,
        *,
        returns_rows: bool = False,
    ) -> tuple[int | None, Sequence[Any]]:
        """Checks the tenants the update writes and runs its ``UPDATE`` - built, or a plan's.

        Args:
            connection: The execution's connection.
            sql: The statement.
            parameters: Its parameters.
            visibility: The visibility the execution resolved.
            returns_rows: Whether the statement returns rows of its own (``returning()``).

        Returns:
            The number of updated rows and the returned rows.
        """
        if self.model._meta.tenant_field:
            TenantWriteRules.raise_if_rows_leave_scope(
                self.model, self.update_kwargs, visibility.all_tenants, UPDATE_METHOD_NAME, requires_scope=True
            )
        await UpdateRelationTenants.check_relation_tenants(self, connection, visibility)
        if not sql:
            # Nothing to set builds no statement - no row is updated, on every driver.
            return 0, []
        return await connection.execute(
            sql,
            parameters,
            returns_rows=returns_rows,
            recovery=connection.get_write_recovery(self.model._meta.basetable),
        )
