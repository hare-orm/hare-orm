from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from hare.contrib.factories.field_declarations.declaration import Declaration


class LazyAttribute(Declaration):
    """A value made from the object's other values - filled after every value that isn't a
    ``LazyAttribute``, then in the order they are declared::

        email = LazyAttribute(lambda user: f"{user.name}@example.com")

    Args:
        function: Takes the values made so far, as attributes.
    """

    def __init__(self, function: Callable[[SimpleNamespace], Any]) -> None:
        self.function = function

    def evaluate_from(self, values: dict[str, Any]) -> Any:
        """The value made from the object's other values.

        Args:
            values: The values made so far, by field name.

        Returns:
            The value.
        """
        return self.function(SimpleNamespace(**values))

    def evaluate(self, sequence_number: int) -> Any:
        raise TypeError("A LazyAttribute is made from the object's other values - evaluate_from()")
