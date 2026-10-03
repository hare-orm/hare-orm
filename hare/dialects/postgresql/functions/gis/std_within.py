from hare.dialects.postgresql.fields.gis import PostGISField
from hare.dialects.postgresql.functions.gis.st_distance import STDistance
from hare.query.expressions import (
    Expression,
    ExpressionContext,
    ExpressionResult,
    F,
)
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.functions.function import Function


class STDWithin(Expression):
    """``ST_DWithin(geog_a, geog_b, radius_m)`` - whether ``geog_a`` is within ``radius_m`` meters of
    ``geog_b``. Unlike ``ST_Distance(...) <= radius``, it can use a GiST index.

    Example: ``Place.objects.annotate(near=STDWithin("location", (55.7558, 37.6173),
    5000)).filter(near=True)``
    """

    def __init__(self, field: str, point: Term | tuple[float, float], radius_m: float) -> None:
        self.field = field
        self.point = point
        self.radius_m = radius_m

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The field read, the point bound as its text, then the radius.

        Args:
            context: The context the expression is resolved in.

        Returns:
            The description, None for a point given as a SQL term.
        """
        return STDistance.get_distance_plan_description(STDWithin, self.field, self.point, context, self.radius_m)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        field_result = F(self.field).get_result(expression_context)
        PostGISField.validate_point_against_field(self.point, field_result.output_field, expression_context.model)  # type: ignore[call-overload]
        geography_term = PostGISField.get_geography_term(self.point)
        if isinstance(self.point, tuple):
            STDistance.record_point(expression_context, geography_term)
        term = Function("ST_DWITHIN", field_result.term, geography_term, self.radius_m)
        radius_term = term.args[2]
        if expression_context.value_wrapper_refs is not None:
            expression_context.value_wrapper_refs.append(
                (
                    ValueRefOrigin.ANNOTATION,
                    LiteralValueRef(radius_term) if isinstance(radius_term, ValueWrapper) else None,
                )
            )
        return ExpressionResult(term=term, joins=field_result.joins)
