from __future__ import annotations

from collections.abc import Awaitable, Generator, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.model_cache import ModelCache
from hare.exceptions import (
    FieldError,
    QueryError,
)
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.field import Field
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_values import RelationValues
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.constraints.written_row_checks import WrittenRowChecks
from hare.models.write.update.expression_assignment import ExpressionAssignment
from hare.models.write.update.written_value_check import WrittenValueCheck
from hare.models.write.write_steps import WriteSteps
from hare.query.expressions import Expression, ExpressionContext
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
from hare.query.statements.write.matching_rows_query import MatchingRowsQuery
from hare.query.statements.write.returning.returned_rows import ReturnedRows
from hare.sql import Table
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.models import Model
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.statements.write.returning.update_returning_query import UpdateReturningQuery


class UpdateQuery(MatchingRowsQuery):
    __slots__ = (
        "update_kwargs",
        "_written_value_check",
    )

    ordering_can_reference_annotation_alias: ClassVar[bool] = False

    #: The assigned values come last - a query of the queryset's calls binds them after the calls'.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *MatchingRowsQuery.plan_slots,
        ("_get_update_plan_description", PlanKeyForm.DESCRIBED),
    )
    subquery_plan_slots: ClassVar[DeclaredPlanSlots] = (
        *MatchingRowsQuery.subquery_plan_slots,
        ("_get_update_plan_description", PlanKeyForm.DESCRIBED),
    )

    def __init__(self, source: QuerySpecification[Any], update_kwargs: dict[str, Any]) -> None:
        """
        Args:
            source: The queryset whose rows are updated, or a query made from one.
            update_kwargs: The values to set, by field name.
        """
        super().__init__(source)
        self.update_kwargs = update_kwargs
        #: The fields set from an expression whose written value is checked - worked out by the
        #: build.
        self._written_value_check = WrittenValueCheck(self.model)

    def _prepare_build(self) -> None:
        self._written_value_check = WrittenValueCheck(self.model)

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

    def _restore_from_plan(self, plan: StatementPlan) -> None:
        # Which columns are re-checked from the RETURNING rows is worked out while the values are
        # resolved - kept with the plan.
        self._written_value_check = plan.result_reading

    def _get_plan_record(self) -> dict[str, Any]:
        return {"result_reading": self._written_value_check}

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        table = self.model._meta.basetable
        if self._needs_primary_key_subquery_for_rows():
            self.query = self._connection.query_class.update(table).where(
                self._get_matching_primary_key_criterion(value_wrapper_references)
            )
        else:
            self._make_filtered_update_query(table, value_wrapper_references)
        self._apply_update_values(table, value_wrapper_references)
        # Without RETURNING the written values are checked by a SELECT built from this query - run
        # on its built query, never on a plan.
        return not (self._written_value_check and not self.dialect.features.supports_returning)

    def _get_auto_now_fields(self) -> list[tuple[str, DatetimeField[Any] | TimeField[Any]]]:
        """The ``auto_now`` fields the update doesn't assign itself - each is set to the moment
        of the update.

        Returns:
            ``(field name, field)`` per field, in model field order.
        """
        model_auto_now_fields = UpdateQuery.get_model_auto_now_fields(self.model)
        if not model_auto_now_fields:
            return []
        return [
            (field_name, field_object)
            for field_name, field_object in model_auto_now_fields
            if field_name not in self.update_kwargs
        ]

    @staticmethod
    @ModelCache.fact()
    def get_model_auto_now_fields(model: type[Model]) -> tuple[tuple[str, DatetimeField[Any] | TimeField[Any]], ...]:
        """The ``auto_now`` fields of a model.

        Args:
            model: The model.

        Returns:
            ``(field name, field)`` per field, in model field order.
        """
        return tuple(
            (field_name, field_object)
            for field_name, field_object in model._meta.fields_map.items()
            if isinstance(field_object, DatetimeField | TimeField) and field_object.auto_now
        )

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
                    key_values = self._get_composite_relation_key_values(key, value)
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
                key_values = self._get_composite_relation_key_values(key, value)
                values.extend(key_values)
                if origins is not None:
                    origins += [
                        PlanOrigins.get_value_origin(update_kwargs, key, index) for index in range(len(key_values))
                    ]
                continue
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                # The check the build makes - it depends on the instance, not only on its type.
                RelationValues.validate_relation_type(self.model, key, value)
                values.append(
                    None if value is None else getattr(value, field_object.to_field_instance.model_field_name)
                )
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
            QueryOrdering.get_ordering(self, self.model, table, self._orderings, self._annotations)

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

    def _get_composite_relation_key_values(self, key: str, value: Any) -> tuple[Any, ...]:
        """The key column values an update assigns a relation to a composite key.

        Args:
            key: The relation's name.
            value: A related model instance, a key tuple, or None to clear the relation.

        Returns:
            One value per key column, in key order.

        Raises:
            QueryError: The value is neither a related instance, a matching tuple nor None.
        """
        from hare.models import Model

        field_object = cast("RelationalField[Model]", self.model._meta.fields_map[key])
        column_count = len(field_object.source_fields)
        if value is None:
            return (None,) * column_count
        if isinstance(value, Model):
            RelationValues.validate_relation_type(self.model, key, value)
            to_field_names = [
                to_field_instance.model_field_name for to_field_instance in field_object.to_field_instances
            ]
            return tuple(RelationValues.get_relation_key_values(value, to_field_names, f"Update of '{key}'"))
        if isinstance(value, tuple) and len(value) == column_count:
            return value
        raise QueryError(
            f"'{key}' is a relation to a composite key - update it to a {field_object.related_model.__name__} "
            f"instance, a {column_count}-tuple or None, got {value!r}"
        )

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
            zip(field_object.source_fields, self._get_composite_relation_key_values(key, value), strict=True)
        ):
            source_field_object = self.model._meta.fields_map[source_field_name]
            self._set_recorded(
                source_field_object.source_field or source_field_name,
                self.dialect.types.get_db_value(source_field_object, key_value, None),
                source_field_object,
                value_wrapper_references,
                PlanOrigins.get_value_origin(self.update_kwargs, key, index),
            )

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
            QueryError: The value isn't an object of the related model.
        """
        RelationValues.validate_relation_type(self.model, key, value)
        foreign_key_field: str = field_object.source_field  # type: ignore[assignment]
        value_field_object = self.model._meta.fields_map[foreign_key_field]
        # None clears the relation - there's no related object to read the key from.
        db_value = self.dialect.types.get_db_value(
            value_field_object,
            None if value is None else getattr(value, field_object.to_field_instance.model_field_name),
            None,
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

        Raises:
            FieldError: The field is virtual.
        """
        try:
            db_field = self.model._meta.fields_db_projection[key]
        except KeyError:
            raise FieldError(f"Field {key} is virtual and can not be updated")
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
        self._written_value_check.add(self.dialect, db_field, field_object)
        return db_field, term, None

    def _set_automatic_values(self, table: Table, value_wrapper_references: RecordedValueReferences | None) -> None:
        """Adds the values every update writes whatever it is given: the bump of
        ``Meta.optimistic_lock_field`` and the ``auto_now`` stamps.

        Args:
            table: The base table.
            value_wrapper_references: Where each assigned value's reference is recorded - None when
                the statement keeps no plan.
        """
        if self.update_kwargs and (optimistic_lock_field := self.model._meta.optimistic_lock_field):
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
            FieldError: An unknown or virtual field is given, or a value is an aggregate.
            IntegrityError: A primary key or generated field is given.
            QueryError: A value references a related model's field or a window function.
        """
        for key, value in self.update_kwargs.items():
            field_object = self.model._meta.fields_map.get(key)
            if not field_object:
                raise FieldError(f"Unknown keyword argument {key} for model {self.model}")
            if field_object.pk:
                raise QueryError(f"Field {key} is PK and can not be updated")
            if field_object.generated:
                raise QueryError(f"Field {key} is generated and can not be updated")
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
            if field_object.sensitive:
                # The value written into a sensitive field is never shown (QueryParameters).
                for value_wrapper in self.query._updates[-1][1].find_(ValueWrapper):
                    value_wrapper.sensitive = True

        self._set_automatic_values(table, value_wrapper_references)

        if self._written_value_check and self.dialect.features.supports_returning:
            self.query = self.query.returning(*self._written_value_check.get_returning_columns())
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is not None and capture_needs.captures(RowOperation.UPDATE):
            # The rows' keys and captured fields for Meta.change_capture - as they were too, where the
            # database returns them.
            self.query = self.query.returning(*capture_needs.columns)
            if capture_needs.reads_before and self._connection.features.supports_returning_old_new:
                self.query = self.query.returning(*ChangeCapturing.get_old_value_terms(self.model, capture_needs))

        # Must run last - the joined-subquery branch above replaces self.query with a brand new
        # QueryBuilder (self._db.query_class.update(table)), which would silently drop a WITH
        # clause applied any earlier. Mirrors the select/aggregate paths' identical placement.
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

        return UpdateReturningQuery(self, self.update_kwargs, ReturnedRows(self.model, field_names, old))

    def __await__(self) -> Generator[Any, None, int]:
        # .limit(0) matches no rows - nothing to write.
        if self._is_none or self._limit == 0:
            return self._execute_none().__await__()
        query = self._get_execution_query(True)
        capture_needs = query.model._meta.change_capture_needs
        if capture_needs is not None and capture_needs.captures(RowOperation.UPDATE):
            return query._execute_captured(capture_needs).__await__()
        if query._connection.features.checks_constraints_before_write:
            return query._run_checked_statement().__await__()
        query._make_query_to_run()
        return query._report_update(query._run_statement()).__await__()

    async def _run_checked_statement(self) -> int:
        """Runs the update once the uniqueness and the relations its values break are checked - on a
        database keeping neither.

        Returns:
            How many rows it updated.
        """
        await WrittenRowChecks.check_query_update(self._matching_queryset(), self._connection, self.update_kwargs)
        self._make_query_to_run()
        return await self._report_update(self._run_statement())

    def _get_query_on(self, connection: DatabaseClient) -> UpdateQuery:
        """This update, run on another connection - a transaction opened for it.

        Args:
            connection: The connection.

        Returns:
            The update.
        """
        query = UpdateQuery(self, self.update_kwargs)
        query._apply_connection(connection)
        query._connection_explicitly_chosen = True
        return query

    async def _execute_captured(self, capture_needs: CaptureNeeds) -> int:
        """Runs the update of a model with ``Meta.change_capture`` and captures the rows it wrote, in
        one transaction.

        Args:
            capture_needs: The model's needs.

        Returns:
            How many rows it updated.
        """
        async with ChangeCapturing.transaction(self._connection) as connection:
            query = self if connection is self._connection else self._get_query_on(connection)
            count, raw_rows = await query._run_captured(capture_needs)
        if count:
            pks = [change_pk for change_pk, _tenant in self._get_returned_keys(capture_needs, raw_rows)]
            await WriteSteps.report(
                self._connection, self.model, RowOperation.UPDATE, pks=pks, fields=self.update_kwargs
            )
        return count

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

    async def _run_captured(self, capture_needs: CaptureNeeds) -> tuple[int, Sequence[Any]]:
        """Runs the update - on a transaction - returning its rows, and gives their changes to the
        model's sink. The rows as they were come from ``RETURNING OLD``, else from reading the matched
        rows first, locked, in one query - the update then writes those rows alone, so a row committed
        by another transaction in between is left as it is.

        Args:
            capture_needs: The model's needs.

        Returns:
            The number of updated rows and the returned rows.
        """
        connection = self._connection
        returns_old = capture_needs.reads_before and connection.features.supports_returning_old_new
        before_by_pk: dict[Any, Any] | None = None
        if capture_needs.reads_before and not returns_old:
            # Local import: the queryset package imports the query statements.
            from hare.query.queryset.selection.statement_selection import StatementSelection

            values_by_pk = await ChangeCapturing.read_values_of(
                self.model,
                connection,
                StatementSelection.get_primary_key_values_query(self._matching_queryset()),
                capture_needs,
                lock=True,
            )
            if not values_by_pk:
                return 0, []
            before_by_pk = {pk: values for pk, (values, _tenant) in values_by_pk.items()}
            self._filter_call_counter += 1
            generation = self._filter_call_counter
            PendingFilterCalls.add_filter_conditions(
                self,
                False,
                PendingFilterCalls.get_filter_kwarg_conditions(self, {"pk__in": list(before_by_pk)}, generation),
                generation,
            )
        self._make_query_to_run()
        count, raw_rows = await self._run_statement(returns_rows=True)
        changes = ChangeCapturing.get_returned_changes(
            self.model,
            capture_needs,
            connection.dialect.types,
            raw_rows,
            RowOperation.UPDATE,
            changed=self.update_kwargs,
            returns_old=returns_old,
            before_by_pk=before_by_pk,
        )
        await ChangeCapturing.capture(connection, self.model, changes)
        return count, raw_rows

    async def _report_update(self, update: Awaitable[tuple[int, Sequence[Any]]]) -> int:
        """Runs the update and reports the rows it changed (``ChangeEvents``).

        Args:
            update: The update's statement (``_run_statement()``).

        Returns:
            How many rows it updated.
        """
        count = (await update)[0]
        # _report_updated_rows() written out - an update runs no coroutine more than it needs.
        if count and ChangeEvents.is_observed(self.model):
            await WriteSteps.report(self._connection, self.model, RowOperation.UPDATE, fields=self.update_kwargs)
        return count

    async def _report_updated_rows(self, count: int) -> None:
        """Reports the rows an update changed (``ChangeEvents``).

        Args:
            count: How many rows it updated.
        """
        if count and ChangeEvents.is_observed(self.model):
            await WriteSteps.report(self._connection, self.model, RowOperation.UPDATE, fields=self.update_kwargs)

    async def _execute_none(self) -> int:
        return 0

    async def _execute(self) -> int:
        return (await self._run_statement())[0]

    async def _run_statement(self, *, returns_rows: bool = False) -> tuple[int, Sequence[Any]]:
        """Checks the tenants the update writes and runs the built ``UPDATE`` - checking the values it
        wrote from expressions.

        Args:
            returns_rows: Whether the statement returns rows of its own (``returning()``).

        Returns:
            The number of updated rows and the returned rows.
        """
        if self.model._meta.tenant_field:
            self._raise_if_tenant_update_leaves_scope()
        await self._check_relation_tenants()
        if self._written_value_check:
            if self._connection.features.supports_returning:
                return await self._written_value_check.execute(self._connection, *self._get_parameterized_sql())
            values_query = self._written_value_check.get_values_query(self._connection.query_class, self.query)
            await self._written_value_check.check_before(self._connection, values_query)
        return await self._connection.execute(*self._get_parameterized_sql(), returns_rows=returns_rows)

    def _raise_if_tenant_update_leaves_scope(self) -> None:
        """Rejects an update setting ``Meta.tenant_field`` to anything but a tenant value the
        model's scope allows.

        Raises:
            QueryError: The tenant field is set to a value outside the scope, to None or an
                expression, or the query has no tenant scope.
        """
        from hare.models import Model

        model = self.model
        tenant_field = cast("str", model._meta.tenant_field)
        tenant_relation_key = model._meta.foreign_key_shadow_columns.get(tenant_field)
        tenant_relation_name = tenant_relation_key[1:] if tenant_relation_key else None
        if tenant_field in self.update_kwargs:
            tenant = self.update_kwargs[tenant_field]
        elif tenant_relation_name is not None and tenant_relation_name in self.update_kwargs:
            tenant = self.update_kwargs[tenant_relation_name]
            if isinstance(tenant, Model):
                relation_field = cast("RelationalField[Any]", model._meta.fields_map[tenant_relation_name])
                to_field = relation_field.to_field_instances[relation_field.source_fields.index(tenant_field)]
                tenant = getattr(tenant, to_field.model_field_name)
        else:
            return
        scope = None if self._visibility.all_tenants else Tenancy.get_scope(model)
        if (
            scope is None
            or tenant is None
            or isinstance(tenant, (Expression, Term))
            or not Tenancy.allows(model, scope, tenant)
        ):
            raise QueryError(
                f"Cannot set '{tenant_field}' via .update() - rows move only to a tenant the active "
                f"Tenancy.scope(...) allows {model.__name__}, given as a plain value"
            )

    async def _check_relation_tenants(self) -> None:
        """Rejects an update pointing a relation at a row its model's tenant scope doesn't show.

        Raises:
            QueryError: A relation value names another tenant's row.
        """
        if self._visibility.all_tenants or not Tenancy.has_tenant_scoped_relations(self.model):
            return
        from hare.models import Model

        meta = self.model._meta
        row: dict[str, Any] = {}
        for key, value in self.update_kwargs.items():
            if isinstance(value, (Expression, Term)):
                continue
            field_object = meta.fields_map.get(key)
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                if len(field_object.source_fields) > 1:
                    key_values = self._get_composite_relation_key_values(key, value)
                elif isinstance(value, Model):
                    key_values = (getattr(value, field_object.to_field_instance.model_field_name),)
                else:
                    key_values = (value,)
                row.update(zip(field_object.source_fields, key_values, strict=True))
            else:
                row[key] = value
        await Tenancy.check_relation_targets(self.model, [row], self._connection, set(self.update_kwargs))
