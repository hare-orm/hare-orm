from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.query.expressions.value_references.scalar_value_reference import ScalarValueReference
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.statements.constants import CONTAINS_SOURCE_ALIAS, CONTAINS_SOURCE_COLUMN_ALIAS_PREFIX
from hare.query.statements.projections.derived_projection import DerivedProjection
from hare.query.statements.summary.exists_query import ExistsQuery
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.statements.awaitable_query import AwaitableQuery
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.tables.selectable import Selectable

TModel = TypeVar("TModel", bound="Model")


class ContainsQuery(ExistsQuery):
    """``queryset.contains(obj)`` - whether the queryset's rows hold the object's row."""

    #: The rows' plan, then the object's key compared after it.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *ExistsQuery.plan_slots,
        ("_get_primary_key_values", PlanKeyForm.BOUND_VALUES_METHOD),
    )

    def __init__(
        self, source: QuerySpecification[Any], obj: TModel, *, rows_query: AwaitableQuery[Any] | None = None
    ) -> None:
        """
        Args:
            source: The queryset checked.
            obj: The instance looked for among its rows.
            rows_query: The existing row query to summarize, if one has already been prepared.
        """
        super().__init__(source, rows_query=rows_query)
        self._obj = obj

    async def _execute_bound(self) -> bool:
        from hare.models.instances.instance_origins import InstanceOrigins

        model_binding: ModelBinding = self._binding  # type: ignore[assignment]
        InstanceOrigins.check(model_binding, self._context, self._obj, "contains()")
        return await super()._execute_bound()

    def _get_primary_key_values(self) -> tuple[Any, ...]:
        """The values of the object's primary key - one per column of a composite key."""
        obj_pk = self._obj.pk
        return obj_pk if isinstance(obj_pk, tuple) else (obj_pk,)

    def _get_rows_summary_description(self) -> PlanDescription:
        """Bind membership keys after the selected rows, including on derived-plan hits."""
        values = list(self._get_primary_key_values())
        return PlanDescription(
            type(self),
            values,
            [PlanOrigins.get_value_origin(self, "_get_primary_key_values", index) for index in range(len(values))]
            if PlanOrigins.records
            else None,
        )

    def _get_query_over_rows(
        self, built_rows_query: AwaitableQuery[Any], value_wrapper_references: RecordedValueReferences | None
    ) -> QueryBuilder:
        """Select membership after the inner query has selected each group's representative."""
        projection = DerivedProjection(built_rows_query.query, column_prefix=CONTAINS_SOURCE_COLUMN_ALIAS_PREFIX)
        return (
            self._connection.query_class.from_(projection.query.as_(CONTAINS_SOURCE_ALIAS))
            .select(ValueWrapper(1, allow_parametrize=False))
            .limit(1)
        )

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        if not super()._build_statement(value_wrapper_references, records_for_caller=records_for_caller):
            return False
        # The binding's pk/db_pk_column are only set for a single-column PK (None/"" for a
        # composite one) - build the criterion from every primary_key_attribute_names column instead, so a
        # composite-PK model's .contains(obj) doesn't crash on self._binding.pk being None.
        model_binding: ModelBinding = self._binding  # type: ignore[assignment]
        primary_key_attribute_names = model_binding.primary_key_attribute_names
        pk_field_objs = [model_binding.fields_map[name] for name in primary_key_attribute_names]
        table: Selectable
        if self._bound_rows_query is None:
            table = self._effective_basetable()
            columns = [field.source_field or field.model_field_name for field in pk_field_objs]
        else:
            selected_rows: QueryBuilder = self.query._from[0]  # type: ignore[assignment]
            table = selected_rows
            columns = [term.alias for term in selected_rows._selects[: len(pk_field_objs)]]  # type: ignore[misc]
        criterion = None
        for index, (field_obj, column, value) in enumerate(
            zip(pk_field_objs, columns, self._get_primary_key_values(), strict=True)
        ):
            # Compared as filter(pk=...) compares it - the conversion a plan binds a new key with.
            wrapper = ValueWrapper(self.dialect.types.get_lookup_value(field_obj, value, self._obj))
            column_equality = table[column].eq(wrapper)
            if value_wrapper_references is not None:
                value_origin = PlanOrigins.get_value_origin(self, "_get_primary_key_values", index)
                value_wrapper_references.append((value_origin, ScalarValueReference(wrapper, field_obj)))
            criterion = column_equality if criterion is None else criterion & column_equality
        if criterion is not None:
            self.query = self.query.where(criterion)
        return True
