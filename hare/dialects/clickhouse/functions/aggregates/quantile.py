from __future__ import annotations

from typing import Any, ClassVar, cast

from hare.dialects.clickhouse.functions.aggregates.parametric_aggregate import ParametricAggregate
from hare.exceptions import QueryError
from hare.fields.field import Field
from hare.query.expressions import Aggregate
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class Quantile(ParametricAggregate):
    """``quantile(level)(field)`` - the value below which ``level`` of a group's values lie, the median
    at ``0.5``. An estimate over a sample of the values, interpolated - a float of integers; with
    ``exact=True`` (``quantileExact``) one of the values themselves, found over all of them::

        Order.objects.aggregate(typical=Quantile("total", 0.5), high=Quantile("total", 0.99, exact=True))

    Args:
        field: The field or expression.
        level: The share of the values below the result - from 0 to 1.
        exact: Whether the quantile is found over every value, not estimated.

    Raises:
        QueryError: ``level`` isn't a number from 0 to 1.
    """

    function_name = "quantile"
    populate_field_object = True

    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Aggregate.plan_parts,
        ("level", PlanPartType.KEY),
        ("exact", PlanPartType.KEY),
    )

    def __init__(self, field: Any, level: float = 0.5, *, exact: bool = False, **kwargs: Any) -> None:
        super().__init__(field, **kwargs)
        self.level = self.get_validated_level(level)
        self.exact = exact

    @staticmethod
    def get_validated_level(level: Any) -> float:
        """A quantile level - a number from 0 to 1.

        Args:
            level: The level.

        Returns:
            The level as a float.

        Raises:
            QueryError: It isn't one.
        """
        if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 1:
            raise QueryError(f"A quantile's level is a number from 0 to 1, got {level!r}")
        return float(level)

    def get_parameters(self) -> tuple[Any, ...]:
        return (self.level,)

    def get_function_name(self) -> str:
        return f"{self.function_name}Exact" if self.exact else str(self.function_name)

    def _coerce_output_field(self, field_object: Field[Any]) -> Field[Any]:
        # An exact quantile is one of the values; an estimate of integers is interpolated.
        if self.exact:
            return field_object
        return cast("Field[Any]", NumericTyping.get_quotient_output_field(field_object))
