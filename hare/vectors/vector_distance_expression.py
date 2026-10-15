from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import UnSupportedError
from hare.fields import Field, FloatField
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.term import Term
from hare.vectors.constants import VECTOR_SEARCH_REQUIRED_FEATURE
from hare.vectors.enums import VectorDistanceType
from hare.vectors.terms.vector_distance_term import VectorDistanceTerm
from hare.vectors.terms.vector_literal import VectorLiteral
from hare.vectors.vector_field import VectorField

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.query.expressions.expression_result import TableCriterionTuple


class VectorDistanceExpression(Expression):
    """Base of ``L2Distance``/``CosineDistance``/``InnerProduct`` - the distance of a vector column
    (or an expression) from a query vector (or another column). Smaller is more similar for each of
    them. Needs ``features.supports_vector_search``.

    Args:
        field: A field name, or an already-built ``Term``/``Expression``.
        vector: The query vector - a ``list[float]``/``tuple[float, ...]``, or a
            ``Term``/``Expression`` (another vector column, via ``F("other_embedding")``).
    """

    DISTANCE_TYPE: ClassVar[VectorDistanceType]

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("field", PlanPartType.FIELD),
        ("vector", PlanPartType.ENCODED_ARGUMENT),
    )

    def __init__(self, field: str | Term | Expression, vector: Sequence[float] | Term | Expression) -> None:
        self.field = field
        self.vector = vector

    @staticmethod
    def get_vector_literal(
        target_field: Field[Any] | None, dialect: Dialect, model: Any, vector: Sequence[float]
    ) -> Any:
        """The bound form of a query vector - converted the way the dialect stores the field compared
        with when it is a ``VectorField``, else the way it stores a vector of that length.

        Args:
            target_field: The field compared with.
            dialect: The dialect of the query's connection.
            model: The query's model.
            vector: The vector.

        Returns:
            The bound value.
        """
        vector_field = target_field if isinstance(target_field, VectorField) else VectorField(len(vector))
        return dialect.types.get_db_value(vector_field, vector, model)

    def raise_if_unsupported(self, expression_context: ExpressionContext) -> None:
        """Rejects the distance on a connection without vector search.

        Args:
            expression_context: The context the expression is resolved in.

        Raises:
            UnSupportedError: The connection has no ``features.supports_vector_search``.
        """
        connection = expression_context.connection
        if connection is not None and not getattr(connection.features, VECTOR_SEARCH_REQUIRED_FEATURE):
            raise UnSupportedError(
                f"{type(self).__name__} can't run on the {connection.connection_alias!r} connection: it needs "
                f"features.{VECTOR_SEARCH_REQUIRED_FEATURE}, which the connection doesn't have"
            )

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        self.raise_if_unsupported(expression_context)
        field_result = ExpressionArguments.get_field_result(self, "field", self.field, expression_context)
        vector_result = self._get_vector_operand(expression_context, field_result.output_field)  # type: ignore[call-overload]
        term = VectorDistanceTerm(self.DISTANCE_TYPE, field_result.term, vector_result.term)
        joins: list[TableCriterionTuple] = ExpressionResult.dedup_joins(field_result.joins, vector_result.joins)
        return ExpressionResult(term=term, joins=joins, output_field=FloatField())

    def _get_vector_operand(
        self, expression_context: ExpressionContext, target_field: Field[Any] | None
    ) -> ExpressionResult:
        # Converted and validated through the target field when that is a VectorField.
        encoder = partial(self.get_vector_literal, target_field, expression_context.dialect, expression_context.model)
        result = ExpressionArguments.get_result(
            self, "vector", self.vector, expression_context, encoder=encoder, binds_whole=True
        )
        if isinstance(self.vector, (Expression, Term)):
            return result
        return ExpressionResult(term=VectorLiteral(result.term))
