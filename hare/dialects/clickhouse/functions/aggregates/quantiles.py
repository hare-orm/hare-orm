from __future__ import annotations

from typing import Any, ClassVar, cast

from hare.dialects.clickhouse.functions.aggregates.parametric_aggregate import ParametricAggregate
from hare.dialects.clickhouse.functions.aggregates.quantile import Quantile
from hare.exceptions import QueryError
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class Quantiles(ParametricAggregate):
    """``quantiles(level, ...)(field)`` - several quantiles of a group's values in one pass, an array
    in the order of the levels; estimates, or with ``exact=True`` (``quantilesExact``) values
    themselves::

        Order.objects.aggregate(spread=Quantiles("total", 0.25, 0.5, 0.75))

    Args:
        field: The field or expression.
        *levels: The shares of the values below each result - each from 0 to 1.
        exact: Whether the quantiles are found over every value, not estimated.

    Raises:
        QueryError: No level is given, or one isn't a number from 0 to 1.
    """

    function_name = "quantiles"
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Aggregate.plan_parts,
        ("levels", PlanPartType.KEY),
        ("exact", PlanPartType.KEY),
    )

    def __init__(self, field: Any, *levels: float, exact: bool = False, **kwargs: Any) -> None:
        super().__init__(field, **kwargs)
        if not levels:
            raise QueryError("Quantiles() takes one or more levels")
        self.levels = tuple(Quantile.get_validated_level(level) for level in levels)
        self.exact = exact

    def get_parameters(self) -> tuple[Any, ...]:
        return self.levels

    def get_function_name(self) -> str:
        return f"{self.function_name}Exact" if self.exact else str(self.function_name)

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        quantile_field = (
            field_object if self.exact else cast("Field[Any]", NumericTyping.get_quotient_output_field(field_object))
        )
        return ArrayField(base_field=quantile_field)
