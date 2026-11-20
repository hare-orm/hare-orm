from __future__ import annotations

from dataclasses import replace
from typing import ClassVar

from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.query.expressions import Expression, ExpressionContext, ExpressionResult
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType


class ConfiguredSearchVector(Expression):
    """Apply TSVectorField generation defaults without changing its declared expression tree."""

    declared_dialects = frozenset({PostgresqlDialect})

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("expression", PlanPartType.EXPRESSION),
        ("config", PlanPartType.ARGUMENT),
    )

    def __init__(self, expression: Expression, config: str | None) -> None:
        self.expression = expression
        self.config = config

    def get_result(self, expression_context: ExpressionContext) -> ExpressionResult:
        return self.expression.get_result(
            replace(expression_context, text_search_config=self.config, requires_search_config=True)
        )
