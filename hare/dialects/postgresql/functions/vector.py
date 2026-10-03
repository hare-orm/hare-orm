from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.dialects.postgresql.fields.vector import VectorField
from hare.dialects.postgresql.lookups.declarations import VectorInfixOperator
from hare.dialects.postgresql.search.criterion.search_arguments import SearchArguments
from hare.fields import Field, FloatField
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.functions.cast import Cast
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.utils.declared_subclass import DeclaredSubclass

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.query.expressions.base.expression_result import TableCriterionTuple


class VectorDistanceExpression(Expression):
    """Base for ``L2Distance``/``CosineDistance``/``InnerProduct`` - each wraps
    ``VectorInfixOperator`` with its own operator string, comparing a field (or an already-built
    expression) against a query vector (or another field/expression).

    Args:
        field: A field name (resolved the same way a plain field path in an annotation is), or
            an already-built ``Term``/``Expression``.
        vector: The query vector to compare against - a plain ``list[float]``/``tuple[float,
            ...]`` literal, or an already-built ``Term``/``Expression`` (e.g. another vector
            column, via ``F("other_embedding")``).
    """

    OPERATOR: str

    def __init__(
        self,
        field: str | Term | Expression,
        vector: Sequence[float] | Term | Expression,
    ) -> None:
        self.field = field
        self.vector = vector

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """The field or expression read, then the query vector - a literal one bound, converted
        the way the field converts its values.

        Args:
            context: The context the expression is resolved in.

        Returns:
            The description, None for a field or a vector given as a SQL term.
        """
        if isinstance(self.vector, Expression):
            vector_description = self.vector.get_plan_description(context)
        elif isinstance(self.vector, Term):
            vector_description = None
        else:
            vector_description = PlanDescription("vector", [self.vector])
        return PlanDescription.combine(
            type(self),
            (SearchArguments.get_plan_description(self.field, context, treat_str_as_field=True), vector_description),
        )

    @staticmethod
    def get_vector_literal(
        target_field: Field[Any] | None, dialect: Dialect, model: Any, vector: Sequence[float]
    ) -> Any:
        """The bound form of a query vector - converted by the field compared with when it is a
        ``VectorField``, else formatted as pgvector text.

        Args:
            target_field: The field compared with.
            dialect: The dialect of the query's connection.
            model: The query's model.
            vector: The vector.

        Returns:
            The bound value.
        """
        if isinstance(target_field, VectorField):
            return dialect.types.get_db_value(target_field, vector, model)
        return VectorField.format_vector_text(vector)

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        field_result = SearchArguments.get_result(self.field, expression_context, treat_str_as_field=True)
        vector_result = self._get_vector_operand(expression_context, field_result.output_field)  # type: ignore[call-overload]
        term = VectorInfixOperator(field_result.term, self.OPERATOR, vector_result.term)
        joins: list[TableCriterionTuple] = ExpressionResult.dedup_joins(field_result.joins, vector_result.joins)
        return ExpressionResult(term=term, joins=joins, output_field=FloatField())

    def _get_vector_operand(
        self, expression_context: ExpressionContext, target_field: Field[Any] | None
    ) -> ExpressionResult:
        if isinstance(self.vector, Expression):
            return self.vector.get_result(expression_context)
        if isinstance(self.vector, Term):
            return ExpressionResult(term=self.vector)
        # A list literal is cast to `vector` - a bare parameter would be typed as text. It is
        # converted and validated through the target field when that is a VectorField.
        encoder = partial(self.get_vector_literal, target_field, expression_context.dialect, expression_context.model)
        wrapper = ValueWrapper(encoder(self.vector))
        if expression_context.value_wrapper_refs is not None:
            expression_context.value_wrapper_refs.append(
                (ValueRefOrigin.ANNOTATION, LiteralValueRef(wrapper, encoder))
            )
        return ExpressionResult(term=Cast(wrapper, "vector"))


L2Distance = DeclaredSubclass.make(
    VectorDistanceExpression,
    "L2Distance",
    __name__,
    """Euclidean (L2) distance - pgvector's ``<->`` operator.

    Example: ``Item.objects.annotate(dist=L2Distance("embedding",
    query_vector)).order_by("dist").limit(10)``""",
    OPERATOR=" <-> ",
)


CosineDistance = DeclaredSubclass.make(
    VectorDistanceExpression,
    "CosineDistance",
    __name__,
    """Cosine distance - pgvector's ``<=>`` operator.

    Example: ``Item.objects.annotate(dist=CosineDistance("embedding",
    query_vector)).order_by("dist").limit(10)``""",
    OPERATOR=" <=> ",
)


InnerProduct = DeclaredSubclass.make(
    VectorDistanceExpression,
    "InnerProduct",
    __name__,
    """Negative inner product - pgvector's ``<#>`` operator. Note pgvector defines this as the NEGATIVE
    inner product (so smaller is more similar, consistent with the other two distance expressions here,
    and directly usable with ``.order_by("dist")``).

    Example: ``Item.objects.annotate(dist=InnerProduct("embedding",
    query_vector)).order_by("dist").limit(10)``""",
    OPERATOR=" <#> ",
)
