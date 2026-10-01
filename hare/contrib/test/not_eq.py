from typing import Any

from hare.contrib.test.condition import Condition


class NotEQ(Condition):
    """Matches any value not equal to `value`."""

    def __eq__(self, other: Any) -> bool:
        return self.value != other

    def __str__(self) -> str:
        return f"<!={self.value}>"
