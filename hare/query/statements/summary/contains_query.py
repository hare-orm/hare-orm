from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.query.expressions.value_references.scalar_value_reference import ScalarValueReference
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.statements.summary.exists_query import ExistsQuery
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class ContainsQuery(ExistsQuery):
    #: The rows' plan, then the object's key compared after it.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *ExistsQuery.plan_slots,
        ("_get_primary_key_values", PlanKeyForm.BOUND_VALUES_METHOD),
    )

    def __init__(self, source: QuerySpecification[Any], obj: TModel) -> None:
        """
        Args:
            source: The queryset checked.
            obj: The instance looked for among its rows.
        """
        super().__init__(source)
        self._obj = obj

    def _get_primary_key_values(self) -> tuple[Any, ...]:
        """The values of the object's primary key - one per column of a composite key."""
        obj_pk = self._obj.pk
        return obj_pk if isinstance(obj_pk, tuple) else (obj_pk,)

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        if not super()._build_statement(value_wrapper_references, records_for_caller=records_for_caller):
            return False
        # model._meta.pk/db_pk_column are only set for a single-column PK (None/"" for a
        # composite one) - build the criterion from every primary_key_attribute_names column instead, so a
        # composite-PK model's .contains(obj) doesn't crash on self.model._meta.pk being None.
        primary_key_attribute_names = self.model._meta.primary_key_attribute_names
        pk_field_objs = [self.model._meta.fields_map[name] for name in primary_key_attribute_names]
        table = self._effective_basetable()
        criterion = None
        for index, (field_obj, value) in enumerate(zip(pk_field_objs, self._get_primary_key_values(), strict=True)):
            column = field_obj.source_field or field_obj.model_field_name
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
