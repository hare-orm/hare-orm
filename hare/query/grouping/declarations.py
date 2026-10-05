from __future__ import annotations

from typing import ClassVar

from hare.query.grouping.grouping_set import GroupingSet
from hare.sql.terms.grouping.declarations import CubeElement, RollupElement
from hare.sql.terms.grouping.grouping_element import GroupingElement


class Rollup(GroupingSet):
    """``ROLLUP``: ``Rollup("region", "city")`` groups by region and city, by region alone, and all the
    rows together - subtotals and a grand total."""

    element_class: ClassVar[type[GroupingElement]] = RollupElement


class Cube(GroupingSet):
    """``CUBE``: ``Cube("region", "product")`` groups by every combination of the fields - each alone,
    both, and all the rows together."""

    element_class: ClassVar[type[GroupingElement]] = CubeElement
