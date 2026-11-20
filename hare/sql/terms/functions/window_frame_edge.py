from __future__ import annotations

from typing import ClassVar

from hare.sql.terms.node import Node


class WindowFrameEdge(Node):
    """A frame boundary; None denotes an unbounded edge, zero the current row."""

    structure_attributes: ClassVar[tuple[str, ...]] = ("value",)
    modifier: str

    def __init__(self, value: str | int | None = None) -> None:
        self.value = value

    def __str__(self) -> str:
        value = self.value if self.value is not None else "UNBOUNDED"
        return f"{value} {self.modifier}"
