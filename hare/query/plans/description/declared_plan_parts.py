from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.query.plans.enums import PlanPartType

#: How each attribute of a plannable class meets the plan, in the order of the key - what a class
#: declares as its ``plan_parts``.
type DeclaredPlanParts = tuple[tuple[str, PlanPartType], ...]

#: Runtime parts with each attribute's reader, prepared once by ``PlanDescriberCompiler.compile()``.
#: NONE declarations do not participate in execution.
type BoundPlanParts = tuple[tuple[str, Callable[[Any], Any], PlanPartType], ...]

#: Key-only structure positions read after value-bearing parts, prepared from the same declaration.
type BoundKeyParts = tuple[tuple[int, Callable[[Any], Any], PlanPartType], ...]
