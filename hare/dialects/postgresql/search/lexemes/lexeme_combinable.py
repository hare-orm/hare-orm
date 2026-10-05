from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from hare.query.expressions import ExpressionContext, ExpressionResult
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.search.query.declarations import RawSearchQueryText

if TYPE_CHECKING:
    from hare.dialects.postgresql.search.lexemes.combined_lexeme import CombinedLexeme


class LexemeCombinable(RawSearchQueryText, abstract=True):
    """Gives `Lexeme`/`CombinedLexeme` the ``|``/``&`` operators combining lexemes into a
    raw tsquery (Postgres ``|``/``&``)."""

    #: The raw tsquery text, bound - every attribute of a lexeme is part of it.
    plan_parts: ClassVar[DeclaredPlanParts] = (("_as_tsquery", PlanPartType.VALUE_METHOD),)

    def get_tsquery_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        """Resolves the raw tsquery text, recording where it sits while a query records its plan.

        Args:
            expression_context: The context the lexeme is resolved in.

        Returns:
            The text, bound as a parameter.
        """
        return ExpressionArguments.get_result(self, "_as_tsquery", self._as_tsquery(), expression_context)

    def _combine(self, other: LexemeCombinable, operator: str, reversed: bool) -> CombinedLexeme:
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.search.lexemes.combined_lexeme import CombinedLexeme

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
