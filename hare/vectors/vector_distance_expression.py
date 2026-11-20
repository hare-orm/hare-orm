from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.fields import Field, FloatField
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.term import Term
from hare.sql.terms.values.null_value import NullValue
from hare.vectors.constants import VECTOR_SEARCH_REQUIRED_FEATURE
from hare.vectors.enums import VectorDistanceType
from hare.vectors.terms.vector_distance_term import VectorDistanceTerm
from hare.vectors.terms.vector_literal import VectorLiteral
from hare.vectors.vector_field import VectorField

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.query.expressions.expression_result import TableCriterionTuple


class VectorDistanceExpression(Expression):
    """Base of ``L2Distance``/``CosineDistance``/``InnerProduct`` - the distance of a vector column
    (or an expression) from a query vector (or another column). Smaller is more similar for each of
    them. Needs ``features.supports_vector_search``.

    Args:
        field: A field name, or an already-built ``Term``/``Expression``.
        vector: The query vector - a ``list[float]``/``tuple[float, ...]``, or a
            ``Term``/``Expression`` (another vector column, via ``F("other_embedding")``); None gives
            a NULL distance.
    """

    DISTANCE_TYPE: ClassVar[VectorDistanceType]

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("field", PlanPartType.FIELD),
        ("vector", PlanPartType.ENCODED_ARGUMENT),
    )

    def __init__(self, field: str | Term | Expression, vector: Sequence[float] | Term | Expression | None) -> None:
        self.field = field
        self.vector = vector

    @staticmethod
    def get_vector_literal(dialect: Dialect, model: Any, vector: Sequence[float]) -> Any:
        """Convert a vector of this length when the target has no vector field.

        Args:
            dialect: The dialect of the query's connection.
            model: The query's model.
            vector: The vector.

        Returns:
            The bound value.
        """
        return dialect.types.get_db_value(VectorField(len(vector)), vector, model)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        expression_context.require_connection_feature(type(self).__name__, VECTOR_SEARCH_REQUIRED_FEATURE)
        field_result = ExpressionArguments.get_field_result(self, "field", self.field, expression_context)
        if self.vector is None:
            # The distance from no vector is NULL - a NULL operand, tested before a vector function
            # would refuse it; the distance keeps its type for ORDER BY.
            term = VectorDistanceTerm(self.DISTANCE_TYPE, field_result.term, NullValue())
            return ExpressionResult(term=term, joins=field_result.joins, output_field=FloatField())
        vector_result = self._get_vector_operand(expression_context, field_result.output_field)
        term = VectorDistanceTerm(self.DISTANCE_TYPE, field_result.term, vector_result.term)
        joins: list[TableCriterionTuple] = ExpressionResult.dedup_joins(field_result.joins, vector_result.joins)
        return ExpressionResult(term=term, joins=joins, output_field=FloatField())

    def _get_vector_operand(
        self, expression_context: ExpressionContext, target_field: Field[Any] | None
    ) -> ExpressionResult:
        # Converted and validated through the effective output of a generated column too.
        if target_field is not None:
            target_field = GeneratedField.get_effective_field(target_field)
        encoder = (
            expression_context.dialect.types.get_field_encoder(target_field, expression_context.model)
            if isinstance(target_field, VectorField)
            else partial(self.get_vector_literal, expression_context.dialect, expression_context.model)
        )
        result = ExpressionArguments.get_result(
            self, "vector", self.vector, expression_context, encoder=encoder, binds_whole=True
        )
        if isinstance(self.vector, (Expression, Term)):
            return result
        return ExpressionResult(term=VectorLiteral(result.term))
