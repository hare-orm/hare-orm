from __future__ import annotations

from typing import TYPE_CHECKING

from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.postgresql.search.combined_lexeme import CombinedLexeme


class LexemeCombinable(Expression):
    """Mixin giving `Lexeme`/`CombinedLexeme` the ``|``/``&`` operators to combine lexemes into a
    raw tsquery (Postgres ``|``/``&``)."""

    def get_plan_description(self, context: PlanContext) -> PlanDescription:
        """The raw tsquery text, bound.

        Args:
            context: The context the lexeme is resolved in.

        Returns:
            The description.
        """
        return PlanDescription((LexemeCombinable,), [self._as_tsquery()])

    def get_tsquery_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves the raw tsquery text, recording where it sits while a query records its plan.

        Args:
            expression_context: The context the lexeme is resolved in.

        Returns:
            The text, bound as a parameter.
        """
        wrapper = ValueWrapper(self._as_tsquery())
        if expression_context.value_wrapper_refs is not None:
            expression_context.value_wrapper_refs.append((ValueRefOrigin.ANNOTATION, LiteralValueRef(wrapper)))
        return ExpressionResult(term=wrapper)

    def _combine(self, other: LexemeCombinable, operator: str, reversed: bool) -> CombinedLexeme:
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.search.combined_lexeme import CombinedLexeme

        if not isinstance(other, LexemeCombinable):
            raise TypeError(f"A Lexeme can only be combined with another Lexeme, got {other.__class__.__name__}.")
        if reversed:
            return CombinedLexeme(other, operator, self)
        return CombinedLexeme(self, operator, other)

    def __or__(self, other: LexemeCombinable) -> CombinedLexeme:
        return self._combine(other, " | ", False)

    def __ror__(self, other: LexemeCombinable) -> CombinedLexeme:
        return self._combine(other, " | ", True)

    def __and__(self, other: LexemeCombinable) -> CombinedLexeme:
        return self._combine(other, " & ", False)

    def __rand__(self, other: LexemeCombinable) -> CombinedLexeme:
        return self._combine(other, " & ", True)

    def _as_tsquery(self) -> str:
        raise NotImplementedError

    def __invert__(self) -> LexemeCombinable:
        raise NotImplementedError
