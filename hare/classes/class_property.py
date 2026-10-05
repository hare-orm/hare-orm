from __future__ import annotations

from collections.abc import Callable
from typing import Any


class classproperty:  # named like the built-in property it mirrors
    """A read-only property of a class, read on the class itself (``Hare.apps``).

    Args:
        getter: Called with the class.
    """

    def __init__(self, getter: Callable[[Any], Any]) -> None:
        self.getter = getter

    def __get__(self, instance: Any, owner: type | None = None) -> Any:
        return self.getter(owner)
