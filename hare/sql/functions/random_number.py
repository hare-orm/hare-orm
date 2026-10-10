from __future__ import annotations

from hare.sql.terms.functions.function import Function


class RandomNumber(Function):
    """A random float from 0 (included) to 1 (excluded), new for every row - each dialect writes
    its own function."""

    requires_dialect_renderer = True

    def __init__(self, alias: str | None = None) -> None:
        super().__init__("RANDOM", alias=alias)
