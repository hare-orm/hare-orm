from __future__ import annotations

from typing import Any, ClassVar

from hare.dialects.clickhouse.functions.aggregates.parametric_aggregate import ParametricAggregate
from hare.exceptions import QueryError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class TopK(ParametricAggregate):
    """``topK(k)(field)`` - about the ``k`` most frequent values of a group, an array from the most
    frequent one; an estimate ClickHouse finds in one pass, exact only for few distinct values::

        Event.objects.aggregate(popular=TopK("page", 5))

    Args:
        field: The field or expression.
        k: How many values are kept.

    Raises:
        QueryError: ``k`` isn't a positive int.
    """

    function_name = "topK"
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (*Aggregate.plan_parts, ("k", PlanPartType.KEY))

    def __init__(self, field: Any, k: int = 10, **kwargs: Any) -> None:
        super().__init__(field, **kwargs)
        if type(k) is not int or k < 1:
            raise QueryError(f"TopK(k=...) takes a positive int, got {k!r}")
        self.k = k

    def get_parameters(self) -> tuple[Any, ...]:
        return (self.k,)

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        return ArrayField(base_field=field_object)
