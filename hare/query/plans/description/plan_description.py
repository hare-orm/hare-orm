from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from hare.query.plans.plan_origins import PlanOrigins


@dataclass(slots=True)
class PlanDescription:
    """What a part of a query gives the query's plan: the structure, which is part of the plan's key,
    the values the plan binds, and - only while a query records its plan (``PlanOrigins.records``) -
    the origin of each value. Never changed once made; not frozen, as a frozen dataclass is slower to
    make.

    Args:
        structure: The structure.
        values: The values.
        origins: The origin of each value, None when they aren't listed.
    """

    structure: Any
    values: list[Any]
    origins: list[Any] | None = None

    #: The description of a part holding nothing - no structure, no values.
    EMPTY: ClassVar[PlanDescription]

    #: The description of an optional argument not given.
    ABSENT: ClassVar[PlanDescription]

    @classmethod
    def combine(cls, head: Any, parts: Iterable[PlanDescription | None]) -> PlanDescription | None:
        """The description of a part made of parts described in order - their structures after
        ``head``, their values and origins one after the other.

        Args:
            head: What tells the part apart from other parts - its class and options.
            parts: The parts' descriptions; None for a part keeping no plan.

        Returns:
            The description, None when a part keeps no plan. Its origins are None when a part
            listed none.
        """
        structures: list[Any] = [head]
        values: list[Any] = []
        origins: list[Any] | None = [] if PlanOrigins.records else None
        for part in parts:
            if part is None:
                return None
            structures.append(part.structure)
            values.extend(part.values)
            if origins is not None:
                part_origins = part.origins
                if part_origins is None:
                    origins = None if part.values else origins
                else:
                    origins.extend(part_origins)
        return cls(tuple(structures), values, origins)


PlanDescription.EMPTY = PlanDescription((), [], [])


PlanDescription.ABSENT = PlanDescription(None, [], [])
