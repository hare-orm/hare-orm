from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class PostDeclaration(abc.ABC):
    """What a factory does once its object is created - rows pointing at it, its many-to-many
    links. A built object, never saved, gets none of it."""

    @abc.abstractmethod
    async def apply(self, obj: Model, name: str, given: Any) -> None:
        """Does it for the created object.

        Args:
            obj: The object.
            name: The declaration's name on the factory.
            given: What ``create()`` was given under the name, None for nothing.
        """
