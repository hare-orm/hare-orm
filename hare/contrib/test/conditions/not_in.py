from __future__ import annotations

from typing import Any

from hare.contrib.test.conditions.condition import Condition


class NotIn(Condition):
    """Matches any value that is none of the given arguments."""

    def __init__(self, *args: Any) -> None:
        super().__init__(args)

    # Equal to many values - no hash agrees with that.
    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, other: Any) -> bool:
        return other not in self.value

    def __str__(self) -> str:
        return f"<not in {self.value}>"
