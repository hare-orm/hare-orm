from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: nocoverage
    pass


@dataclass
class AggregateCrossing:
    """To-many relation paths crossed by one aggregate expression.

    Attributes:
        distinct: Whether the aggregate was declared with distinct=True, or ignores repeated rows.
        aggregate_name: The aggregate's class name, e.g. ``Sum``.
        argument_paths: Paths crossed by the aggregated argument itself.
        filter_paths: Paths crossed only by the aggregate's own _filter condition.
    """

    distinct: bool
    aggregate_name: str = ""
    argument_paths: list[str] = dataclass_field(default_factory=list)
    filter_paths: list[str] = dataclass_field(default_factory=list)
