from __future__ import annotations

from collections.abc import Awaitable, Generator
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import (
    FieldError,
    QueryError,
)
from hare.fields.base.field import Field
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.instrumentation.enums import RowOperation
from hare.models.tenancy import Tenancy
from hare.models.write.expression_assignment import ExpressionAssignment
from hare.models.write.write_steps import WriteSteps
from hare.models.write.written_value_check import WrittenValueCheck
from hare.query.expressions import Expression, ExpressionContext, Q
from hare.query.expressions.base.term_expression import TermExpression
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.expressions.value_refs.write_value_ref import WriteValueRef
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.queryset.query_spec import QuerySpec
from hare.query.statements.write.matching_rows_query import MatchingRowsQuery
from hare.sql import Table
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.plans.statement_plan import StatementPlan


class UpdateQuery(MatchingRowsQuery):
    __slots__ = (
        "update_kwargs",
        "_written_value_check",
    )

    ordering_can_reference_annotation_alias: ClassVar[bool] = False

    def __init__(self, source: QuerySpec[Any], update_kwargs: dict[str, Any]) -> None:
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

    def _get_build_plan_description(self) -> PlanDescription | None:
        update_value_structures = self._get_update_value_structures()
        if update_value_structures is None:
            return None
        description = self._get_write_plan_description(update_value_structures)
        if description is None:
            return None
        # The rows' values, then the assigned ones, then the CTEs' - the order the build records.
        return PlanDescription(
            description.structure, description.values + self._get_update_values() + self._get_ctes_values()
        )

    def _get_call_signature_kind_part(self) -> tuple[Any, list[Any]]:
        # The assigned values - their structure in the key, the values bound after the filters'.
        update_value_structures = self._get_update_value_structures()
        if update_value_structures is None or self._get_ctes_values():
            return None, []
        return update_value_structures, self._get_update_values()

    def _restore_from_plan(self, plan: StatementPlan) -> None:
        # Which columns are re-checked from the RETURNING rows is worked out while the values are
        # resolved - kept with the plan.
        self._written_value_check = plan.result_reading

    def _get_plan_record(self) -> dict[str, Any]:
        return {"result_reading": self._written_value_check}

    def _build_statement(self, value_wrapper_refs: RecordedValueRefs | None, *, records_for_caller: bool) -> bool:
        table = self.model._meta.basetable
        if self._needs_primary_key_subquery_for_rows():
            self.query = self._db.query_class.update(table).where(
                self._get_matching_primary_key_criterion(value_wrapper_refs)
            )
        else:
            self._make_filtered_update_query(table, value_wrapper_refs)
        self._apply_update_values(table, value_wrapper_refs)
        return True

    def _get_auto_now_fields(self) -> list[tuple[str, DatetimeField[Any] | TimeField[Any]]]:
        """The ``auto_now`` fields the update doesn't assign itself - each is set to the moment
        of the update.

        Returns:
            ``(field name, field)`` per field, in model field order.
        """
        return [
            (field_name, field_object)
            for field_name, field_object in self.model._meta.fields_map.items()
            if isinstance(field_object, DatetimeField | TimeField)
            and field_object.auto_now
            and field_name not in self.update_kwargs
        ]

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
            value = Q.get_literal_value(value)
            if isinstance(value, Expression):
                value_description = value.get_plan_description(PlanContext.EMPTY)
                if value_description is None:
                    return None
                structures.append((key, value_description.structure))
            elif isinstance(value, Term):
                return None
            else:
                structures.append((key, type(value)))
        return tuple(structures)

    def _get_update_values(self) -> list[Any]:
        """The values the statement assigns, in the order their references are recorded: each given
        value, then the moment every ``auto_now`` field is set to.

        Returns:
            The values.

        Raises:
            ValidationError: A relation is assigned an instance of another model.
            QueryError: A relation is assigned an unsaved instance.
        """
        fields_map = self.model._meta.fields_map
        values: list[Any] = []
        for key, value in self.update_kwargs.items():
            field_object = fields_map[key]
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)) and (
                len(field_object.source_fields) > 1
            ):
                values.extend(self._get_composite_relation_key_values(key, value))
                continue
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                # The check the build makes - it depends on the instance, not only on its type.
                self.model._validate_relation_type(key, value)
                values.append(
                    None if value is None else getattr(value, field_object.to_field_instance.model_field_name)
                )
                continue
            value = Q.get_literal_value(value)
            if isinstance(value, Expression):
                values.extend(cast("PlanDescription", value.get_plan_description(PlanContext.EMPTY)).values)
            else:
                values.append(value)
        auto_now_fields = self._get_auto_now_fields()
        if auto_now_fields:
            now = Timezone.now()
            values.extend(field_object.get_auto_now_value(now) for _field_name, field_object in auto_now_fields)
        return values

    def _set_recorded(
        self,
        db_column: str,
        value: Any,
        field_object: Field[Any],
        value_wrapper_refs: RecordedValueRefs | None,
    ) -> None:
        """Adds ``db_column = value`` to the SET clause, recording where the value sits.

        Args:
            db_column: The assigned column.
            value: The value, converted for the write.
            field_object: The field a later statement's value is converted by.
            value_wrapper_refs: The references being recorded, None when the statement keeps no
                plan. A value that isn't bound as a parameter (NULL, a list) records none.
        """
        self.query = self.query.set(db_column, value)
        if value_wrapper_refs is not None:
            assigned_term = self.query._updates[-1][1]
            value_wrapper_refs.append(
                (
                    db_column,
                    WriteValueRef(assigned_term, field_object) if isinstance(assigned_term, ValueWrapper) else None,
                )
            )

    def _make_filtered_update_query(self, table: Table, value_wrapper_refs: RecordedValueRefs | None) -> None:
        """Builds the UPDATE with this query's own filters - inline when they only read the base
        table, through a ``pk IN (SELECT ...)`` subquery when they join another one.

        Args:
            table: The base table.
            value_wrapper_refs: Where the filters' value references are recorded, None when the
                statement keeps no plan.
        """
        self.query = self._get_base_query()
        if self._limit is not None:
            self.query._limit = self._limit_term = self.query._wrapper_cls(self._limit)
            self.get_ordering(self.model, table, self._orderings, self._annotations)

        self.get_filters(value_wrapper_refs=value_wrapper_refs)
        self._record_matching_rows_limit(value_wrapper_refs)
        # An aggregate annotation in the filters needs its GROUP BY, or the HAVING aggregates over
        # the whole joined result.
        self._apply_auto_group_by()
        if self._filters_need_primary_key_subquery():
            # An UPDATE takes no JOINs - the rows are picked by a primary key subquery. The
            # annotation terms get_filters() selected would become extra subquery columns.
            self.query._selects = []
            matching_rows_criterion = self._get_matching_rows_criterion(self.query)
            self.query = self._db.query_class.update(table)
            self.query = self.query.where(matching_rows_criterion)

        else:
            update_query = self._db.query_class.update(table)
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
            self.model._validate_relation_type(key, value)
            to_field_names = [
                to_field_instance.model_field_name for to_field_instance in field_object.to_field_instances
            ]
            return tuple(value._get_relation_key_values(to_field_names, f"Update of '{key}'"))
        if isinstance(value, tuple) and len(value) == column_count:
            return value
        raise QueryError(
            f"'{key}' is a relation to a composite key - update it to a {field_object.related_model.__name__} "
            f"instance, a {column_count}-tuple or None, got {value!r}"
        )

    def _apply_update_values(self, table: Table, value_wrapper_refs: RecordedValueRefs | None) -> None:
        """Adds the SET clause - the given values, plus the automatic version/auto_now bumps -
        and the WITH clause to the built UPDATE.

        Args:
            table: The base table.
            value_wrapper_refs: Where each assigned value's reference is recorded, in the order
                ``_get_update_values()`` lists the values - None when the statement keeps no
                plan.

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
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)) and (
                len(field_object.source_fields) > 1
            ):
                # A relation to a composite key sets every one of its key columns.
                for source_field_name, key_value in zip(
                    field_object.source_fields, self._get_composite_relation_key_values(key, value), strict=True
                ):
                    source_field_object = self.model._meta.fields_map[source_field_name]
                    self._set_recorded(
                        source_field_object.source_field or source_field_name,
                        self.dialect.types.get_db_value(source_field_object, key_value, None),
                        source_field_object,
                        value_wrapper_refs,
                    )
                continue
            value_field_object: Field[Any] | None = field_object
            if isinstance(field_object, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                self.model._validate_relation_type(key, value)
                fk_field: str = field_object.source_field  # type: ignore[assignment]
                value_field_object = self.model._meta.fields_map[fk_field]
                db_field = value_field_object.source_field
                # None clears the relation - there's no related object to read the key from.
                value = self.dialect.types.get_db_value(
                    value_field_object,
                    None if value is None else getattr(value, field_object.to_field_instance.model_field_name),
                    None,
                )
            else:
                try:
                    db_field = self.model._meta.fields_db_projection[key]
                except KeyError:
                    raise FieldError(f"Field {key} is virtual and can not be updated")

                if isinstance(value, Term) and not isinstance(value, Expression):
                    # A raw SQL fragment (RawSQL(...)) is embedded as is, like an expression.
                    value = TermExpression(value)
                # A literal is converted and validated by the field the way the same plain value
                # is, instead of reaching the driver as is.
                value = Q.get_literal_value(value)
                if isinstance(value, Expression):
                    value = ExpressionAssignment.get_term(
                        key,
                        field_object,
                        value,
                        ExpressionContext(
                            model=self.model,
                            dialect=self.dialect,
                            connection=self._db,
                            table=table,
                            annotations=self._annotations,
                            value_wrapper_refs=value_wrapper_refs,
                        ),
                    )
                    # The expression recorded the references of its own literals.
                    value_field_object = None
                    self._written_value_check.add(self.dialect, db_field, field_object)
                else:
                    value = self.dialect.types.get_db_value(self.model._meta.fields_map[key], value, None)

            if value_field_object is None:
                self.query = self.query.set(db_field, value)
            else:
                self._set_recorded(cast("str", db_field), value, value_field_object, value_wrapper_refs)

        if self.update_kwargs and (optimistic_lock_field := self.model._meta.optimistic_lock_field):
            # The optimistic lock field is bumped in the SET clause (version = version + 1):
            # update() has read no row whose version it could compare.
            version_db_column = self.model._meta.fields_db_projection[optimistic_lock_field]
            self.query = self.query.set(version_db_column, table[version_db_column] + 1)

        # auto_now fields are always written - every row gets the same moment.
        auto_now_fields = self._get_auto_now_fields()
        if auto_now_fields:
            now = Timezone.now()
            for field_name, auto_now_field_object in auto_now_fields:
                self._set_recorded(
                    self.model._meta.fields_db_projection[field_name],
                    self.dialect.types.get_db_value(
                        auto_now_field_object, auto_now_field_object.get_auto_now_value(now), None
                    ),
                    auto_now_field_object,
                    value_wrapper_refs,
                )

        if self._written_value_check:
            self.query = self.query.returning(*self._written_value_check.get_returning_columns())

        # Must run last - the joined-subquery branch above replaces self.query with a brand new
        # QueryBuilder (self._db.query_class.update(table)), which would silently drop a WITH
        # clause applied any earlier. Mirrors the select/aggregate paths' identical placement.
        self._apply_with_ctes(value_wrapper_refs=value_wrapper_refs)

    def __await__(self) -> Generator[Any, None, int]:
        # .limit(0) matches no rows - nothing to write.
        if self._is_none or self._limit == 0:
            return self._execute_none().__await__()
        query = self._get_execution_query(True)
        query._make_query_to_run()
        return query._report_update(query._execute()).__await__()

    async def _report_update(self, update: Awaitable[int]) -> int:
        """Runs the update and reports the rows it changed (``ChangeEvents``).

        Args:
            update: The update.

        Returns:
            How many rows it updated.
        """
        count = await update
        if count:
            await WriteSteps.report(self._db, self.model, RowOperation.UPDATE, fields=self.update_kwargs)
        return count

    async def _execute_none(self) -> int:
        return 0

    async def _execute(self) -> int:
        if self.model._meta.tenant_field:
            self._raise_if_tenant_update_leaves_scope()
        await self._check_relation_tenants()
        if self._written_value_check:
            return (await self._written_value_check.execute(self._db, *self._get_parameterized_sql()))[0]
        return (await self._db.execute(*self._get_parameterized_sql(), returns_rows=False))[0]

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
        tenant_relation_key = model._meta.fk_shadow_columns.get(tenant_field)
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
        from hare.models import Model

        if self._visibility.all_tenants or not Tenancy.has_tenant_scoped_relations(self.model):
            return
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
        await Tenancy.check_relation_targets(self.model, [row], self._db, set(self.update_kwargs))
