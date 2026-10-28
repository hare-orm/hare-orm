from __future__ import annotations

from functools import partial
from typing import Any, ClassVar

from hare.dialects.postgresql.fields.postgis_field import PostGISField
from hare.query.expressions import (
    Expression,
    ExpressionContext,
    ExpressionResult,
    F,
)
from hare.query.expressions.dialect_expressions import DialectExpressions
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term


class STDistance(Expression):
    """``ST_Distance(geog_a, geog_b)`` - the distance in meters between two geography points. ``field``
    is a field name.

    Example: ``Place.objects.annotate(dist=STDistance("location", (55.7558,
    37.6173))).order_by("dist")``
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("field", PlanPartType.KEY),
        ("point", PlanPartType.ENCODED_ARGUMENT),
    )

    def __init__(self, field: str, point: Term | tuple[float, float]) -> None:
        self.field = field
        self.point = point

    @staticmethod
    def get_point_term(expression: Any, target_field: Any, expression_context: ExpressionContext) -> Term:
        """The geography of an expression's point (``point``) - ``ST_GeogFromText()`` of a
        ``(latitude, longitude)`` pair's text, validated through the field and bound; a term (another
        geography column) as it is.

        Args:
            expression: The expression holding the point.
            target_field: The field the point is compared with.
            expression_context: The context the expression is resolved in.

        Returns:
            The term.
        """
        point = expression.point
        # The encoder validates - a reused plan runs it on each new point, not get_result().
        encoder = partial(PostGISField.get_validated_point_text, target_field, expression_context.model)
        point_result = ExpressionArguments.get_result(
            expression, "point", point, expression_context, encoder=encoder, binds_whole=True
        )
        return Function("ST_GEOGFROMTEXT", point_result.term) if isinstance(point, tuple) else point_result.term

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        DialectExpressions.raise_if_dialect_lacks(self, expression_context.dialect)
        field_result = F(self.field).get_result(expression_context)
        point_term = self.get_point_term(self, field_result.output_field, expression_context)
        term = Function("ST_DISTANCE", field_result.term, point_term)
        return ExpressionResult(term=term, joins=field_result.joins)
