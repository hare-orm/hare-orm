from collections.abc import Iterable, Iterator
from typing import Any


class SinglePassValues:
    """Membership input whose iterator must be requested exactly once."""

    def __init__(self, values: Iterable[Any]) -> None:
        self.values = values
        self.iterations = 0

    def __iter__(self) -> Iterator[Any]:
        self.iterations += 1
        if self.iterations > 1:
            raise AssertionError("Membership input was consumed more than once")
        return iter(self.values)
