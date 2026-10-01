from typing import Any

from hare.dialects.postgresql.fields.gis import PostGISField
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


class STDistance(Expression):
    """``ST_Distance(geog_a, geog_b)`` - the distance in meters between two geography points. ``field``
    is a field name.

    Example: ``Place.objects.annotate(dist=STDistance("location", (55.7558,
    37.6173))).order_by("dist")``
    """

    def __init__(self, field: str, point: Term | tuple[float, float]) -> None:
        self.field = field
        self.point = point

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The field read, then the point - a ``(latitude, longitude)`` pair bound as its text.

        Args:
            context: The context the expression is resolved in.

        Returns:
            The description, None for a point given as a SQL term.
        """
        return STDistance.get_distance_plan_description(STDistance, self.field, self.point, context)

    @staticmethod
    def get_distance_plan_description(
        expression_class: type, field: str, point: Term | tuple[float, float], context: PlanContext, *arguments: Any
    ) -> PlanDescription | None:
        """Describes a PostGIS expression over a field and a point.

        Args:
            expression_class: The expression's class.
            field: The field read.
            point: The point.
            context: The context the expression is resolved in.
            arguments: Its other literal arguments.

        Returns:
            The description, None for a point given as a SQL term or an argument keeping no plan.
        """
        if not isinstance(point, tuple):
            return None
        return PlanDescription.combine(
            (expression_class, field),
            (
                PlanDescription((), [*Expression.get_referenced_annotation_values(field, context), point]),
                *(Expression.get_argument_plan_description(argument, context) for argument in arguments),
            ),
        )

    @staticmethod
    def record_point(expression_context: ExpressionContext, geography_term: Term) -> None:
        """Records where a point's text sits, while a query records its plan.

        Args:
            expression_context: The context the expression is resolved in.
            geography_term: The ``ST_GEOGFROMTEXT(...)`` term built from the point.
        """
        value_wrapper_refs = expression_context.value_wrapper_refs
        if value_wrapper_refs is None:
            return
        point_text = geography_term.args[0] if isinstance(geography_term, Function) else None
        value_wrapper_refs.append(
            (
                ValueRefOrigin.ANNOTATION,
                LiteralValueRef(point_text, PostGISField.get_point_text)
                if isinstance(point_text, ValueWrapper)
                else None,
            )
        )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        field_result = F(self.field).get_result(expression_context)
        PostGISField.validate_point_against_field(self.point, field_result.output_field, expression_context.model)  # type: ignore[call-overload]
        geography_term = PostGISField.get_geography_term(self.point)
        if isinstance(self.point, tuple):
            self.record_point(expression_context, geography_term)
        term = Function("ST_DISTANCE", field_result.term, geography_term)
        return ExpressionResult(term=term, joins=field_result.joins)
