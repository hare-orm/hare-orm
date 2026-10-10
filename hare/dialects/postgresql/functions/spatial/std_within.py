from __future__ import annotations

from typing import ClassVar

from hare.dialects.postgresql.fields.postgis_field import PostGISField
from hare.dialects.postgresql.functions.spatial.st_distance import STDistance
from hare.query.expressions import (
    Expression,
    ExpressionContext,
    ExpressionResult,
    F,
)
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class STDWithin(Expression):
    """``ST_DWithin(geog_a, geog_b, radius_m)`` - whether ``geog_a`` is within ``radius_m`` meters of
    ``geog_b``. Unlike ``ST_Distance(...) <= radius``, it can use a GiST index.

    Example: ``Place.objects.annotate(near=STDWithin("location", (55.7558, 37.6173),
    5000)).filter(near=True)``
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("field", PlanPartType.KEY),
        ("point", PlanPartType.ENCODED_ARGUMENT),
        ("radius_m", PlanPartType.ARGUMENT),
    )

    def __init__(self, field: str, point: Term | tuple[float, float], radius_m: float) -> None:
        self.field = field
        self.point = point
        self.radius_m = radius_m

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        field_result = F(self.field).get_result(expression_context)
        PostGISField.validate_point_against_field(self.point, field_result.output_field, expression_context.model)  # type: ignore[call-overload]
        point_term = STDistance.get_point_term(self, expression_context)
        radius_result = ExpressionArguments.get_result(self, "radius_m", self.radius_m, expression_context)
        term = Function("ST_DWITHIN", field_result.term, point_term, radius_result.term)
        return ExpressionResult(term=term, joins=ExpressionResult.dedup_joins(field_result.joins, radius_result.joins))
