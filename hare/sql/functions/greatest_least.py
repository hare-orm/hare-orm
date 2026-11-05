from __future__ import annotations

from typing import Any

from hare.sql.terms.functions.function import Function


class GreatestLeast(Function):
    """``GREATEST``/``LEAST`` - the database's own, or the dialect's function comparing numbers as numbers."""

    structure_attributes = ("compares_numbers",)
    has_repeatable_value = True

    def __init__(self, name: str, *args: Any, compares_numbers: bool = False, alias: str | None = None) -> None:
        """
        Args:
            name: ``GREATEST`` or ``LEAST``.
            args: The values.
            compares_numbers: The values are numbers - compared numerically.
            alias: Optional alias for the term.
        """
        super().__init__(name, *args, alias=alias)
        self.compares_numbers = compares_numbers
