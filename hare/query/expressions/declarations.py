from __future__ import annotations

from typing import ClassVar

from hare.query.expressions.expression import Expression
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts


class ConstantExpression(Expression):
    """An expression whose SQL text takes no value - ``PI()``, the current moment."""

    plan_parts: ClassVar[DeclaredPlanParts] = ()
