from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.query.plans.enums import PlanPartType

#: How each attribute of a plannable class meets the plan, in the order of the key - what a class
#: declares as its ``plan_parts``.
type DeclaredPlanParts = tuple[tuple[str, PlanPartType], ...]

#: The declared parts with the reader of each attribute, made once when the class is compiled
#: (``PlanDescriberCompiler.compile()``).
type BoundPlanParts = tuple[tuple[str, Callable[[Any], Any], PlanPartType], ...]
