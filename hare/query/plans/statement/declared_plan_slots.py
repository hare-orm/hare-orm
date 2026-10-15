from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.query.plans.enums import PlanKeyForm

#: How each setting of a query class meets the key of its plan, in the order of the key - an
#: attribute or method name, or a function taking the query - what a class declares as its
#: ``plan_slots``.
type DeclaredPlanSlots = tuple[tuple[str | Callable[[Any], Any], PlanKeyForm], ...]
