from __future__ import annotations

from typing import Any, ClassVar

from hare.dialects.clickhouse.functions.aggregates.parametric_aggregate import ParametricAggregate
from hare.exceptions import QueryError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class GroupArray(ParametricAggregate):
    """``groupArray(field)`` - a group's values as an array, in the order they are read; NULL values
    are left out (``ArrayAgg`` keeps them). ``max_size`` keeps the first that many
    (``groupArray(n)(field)``)::

        Event.objects.values("user_id").annotate(pages=GroupArray("page", max_size=10))

    Args:
        field: The field or expression.
        max_size: The most values kept - all of them when None.

    Raises:
        QueryError: ``max_size`` isn't a positive int.
    """

    function_name = "groupArray"
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (*Aggregate.plan_parts, ("max_size", PlanPartType.KEY))

    def __init__(self, field: Any, *, max_size: int | None = None, **kwargs: Any) -> None:
        super().__init__(field, **kwargs)
        if max_size is not None and (type(max_size) is not int or max_size < 1):
            raise QueryError(f"{type(self).__name__}(max_size=...) takes a positive int, got {max_size!r}")
        self.max_size = max_size

    def get_parameters(self) -> tuple[Any, ...]:
        return () if self.max_size is None else (self.max_size,)

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        return ArrayField(base_field=field_object)
