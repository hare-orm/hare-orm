from __future__ import annotations

from hare.query.plans.enums import PlanPartType

#: How each attribute of a plannable class meets the plan, in the order of the key - what a class
#: declares as its ``plan_parts``.
type DeclaredPlanParts = tuple[tuple[str, PlanPartType], ...]
