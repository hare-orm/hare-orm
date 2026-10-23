from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.contrib.factories.field_declarations.declaration import Declaration


class Sequence(Declaration):
    """A value made from the object's number in its factory's sequence - unique per object::

        email = Sequence(lambda number: f"user{number}@example.com")

    Args:
        function: Takes the number - 0 for the factory's first object.
    """

    def __init__(self, function: Callable[[int], Any]) -> None:
        self.function = function

    def evaluate(self, sequence_number: int) -> Any:
        return self.function(sequence_number)
