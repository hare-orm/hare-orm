from __future__ import annotations

from typing import ClassVar

from hare.query.expressions.arithmetic.combinable_expression import CombinableExpression
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.expression_result import ExpressionResult
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms.term import Term


class TermExpression(CombinableExpression):
    """A raw SQL term (``RawSQL(...)``, a ``hare.sql`` term) used as an expression - embedded in the
    SQL as is; a plan binds the values it binds as parameters (``TermPlanDescriptions``).

    Args:
        term: The term.
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (("term", PlanPartType.ARGUMENT),)

    def __init__(self, term: Term) -> None:
        self.term = term

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return ExpressionArguments.get_result(self, "term", self.term, expression_context)
