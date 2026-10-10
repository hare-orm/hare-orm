from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hare.contrib.factories.field_declarations.declaration import Declaration


class LazyFunction(Declaration):
    """A value a function makes anew for each object::

        created_at = LazyFunction(datetime.now)

    Args:
        function: Takes nothing.
    """

    def __init__(self, function: Callable[[], Any]) -> None:
        self.function = function

    def evaluate(self, sequence_number: int) -> Any:
        return self.function()
