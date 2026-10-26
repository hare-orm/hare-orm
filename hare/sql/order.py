from __future__ import annotations

from enum import StrEnum


class Order(StrEnum):
    """Sort direction, optionally with an explicit NULL placement - the plain members leave the
    NULL placement to the dialect's own default."""

    ASC = "ASC"
    DESC = "DESC"
    ASC_NULLS_FIRST = "ASC NULLS FIRST"
    ASC_NULLS_LAST = "ASC NULLS LAST"
    DESC_NULLS_FIRST = "DESC NULLS FIRST"
    DESC_NULLS_LAST = "DESC NULLS LAST"

    @property
    def is_ascending(self) -> bool:
        return self.value.startswith("ASC")

    @property
    def nulls_first(self) -> bool | None:
        """True/False for an explicit NULLS FIRST/NULLS LAST, None when left to the dialect."""
        if self.value.endswith("NULLS FIRST"):
            return True
        if self.value.endswith("NULLS LAST"):
            return False
        return None

    @classmethod
    def build(cls, is_ascending: bool, nulls_first: bool | None = None) -> Order:
        """Builds the member for a direction and an optional explicit NULL placement.

        Args:
            is_ascending: True for ASC, False for DESC.
            nulls_first: True for NULLS FIRST, False for NULLS LAST, None to leave it to the dialect.
        """
        value = "ASC" if is_ascending else "DESC"
        if nulls_first is not None:
            value += " NULLS FIRST" if nulls_first else " NULLS LAST"
        return cls(value)

    def get_reversed(self) -> Order:
        """The exact opposite ordering: the direction is inverted and an explicit NULL placement
        is inverted with it, so the reversed sequence is precisely the original read backwards."""
        nulls_first = self.nulls_first
        return Order.build(not self.is_ascending, None if nulls_first is None else not nulls_first)

    def puts_nulls_first(self, sorts_nulls_first: bool) -> bool:
        """Whether NULLs come first: an explicit placement decides, else the default.

        Args:
            sorts_nulls_first: Whether NULLs come first going up by default - they come last going down.
        """
        nulls_first = self.nulls_first
        if nulls_first is not None:
            return nulls_first
        return sorts_nulls_first if self.is_ascending else not sorts_nulls_first
