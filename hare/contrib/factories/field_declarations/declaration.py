from __future__ import annotations

import abc
from typing import Any


class Declaration(abc.ABC):
    """How a factory fills one field of the object it makes - a value made anew for each object."""

    @abc.abstractmethod
    def evaluate(self, sequence_number: int) -> Any:
        """The field's value for one object.

        Args:
            sequence_number: The object's number in its factory's sequence.

        Returns:
            The value.
        """
