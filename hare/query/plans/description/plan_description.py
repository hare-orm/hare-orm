from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(slots=True)
class PlanDescription:
    """What a part of a query gives the query's plan: the structure, which is part of the plan's key,
    and the values the plan binds, in the order the build records their references. Never changed
    once made; not frozen, as a frozen dataclass is slower to make.

    Args:
        structure: The structure.
        values: The values.
    """

    structure: Any
    values: list[Any]

    #: The description of a part holding nothing - no structure, no values.
    EMPTY: ClassVar[PlanDescription]

    #: The description of an optional argument not given.
    ABSENT: ClassVar[PlanDescription]

    @classmethod
    def combine(cls, head: Any, parts: Iterable[PlanDescription | None]) -> PlanDescription | None:
        """The description of a part made of parts described in order - their structures after
        ``head``, their values one after the other.

        Args:
            head: What tells the part apart from other parts - its class and options.
            parts: The parts' descriptions; None for a part keeping no plan.

        Returns:
            The description, None when a part keeps no plan.
        """
        structures: list[Any] = [head]
        values: list[Any] = []
        for part in parts:
            if part is None:
                return None
            structures.append(part.structure)
            values.extend(part.values)
        return cls(tuple(structures), values)


PlanDescription.EMPTY = PlanDescription((), [])


PlanDescription.ABSENT = PlanDescription(None, [])
