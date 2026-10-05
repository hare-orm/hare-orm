from __future__ import annotations

from typing import Any

from hare.contrib.test.conditions.condition import Condition


class NotEQ(Condition):
    """Matches any value not equal to `value`."""

    # Equal to many values - no hash agrees with that.
    __hash__ = None  # type: ignore[assignment]

    def __eq__(self, other: Any) -> bool:
        return self.value != other

    def __str__(self) -> str:
        return f"<!={self.value}>"
