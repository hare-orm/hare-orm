from __future__ import annotations

from typing import ClassVar

from hare.sql.terms.grouping.grouping_element import GroupingElement


class RollupElement(GroupingElement):
    """``ROLLUP(a, b)`` - the groups of ``(a, b)``, of ``(a)`` and of every row."""

    keyword: ClassVar[str] = "ROLLUP"


class CubeElement(GroupingElement):
    """``CUBE(a, b)`` - the groups of every combination of the terms."""

    keyword: ClassVar[str] = "CUBE"
