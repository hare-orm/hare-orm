from __future__ import annotations

from typing import Any


class HashableValues:
    """Values as parts of a plan or structure key."""

    @staticmethod
    def get_hashable_value(value: Any) -> Any:
        """A filter value as a hashable one equal for equal values - a list/set/dict by its items."""
        if isinstance(value, (list, tuple)):
            return type(value).__name__, tuple(HashableValues.get_hashable_value(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return type(value).__name__, frozenset(HashableValues.get_hashable_value(item) for item in value)
        if isinstance(value, dict):
            return "dict", frozenset((key, HashableValues.get_hashable_value(item)) for key, item in value.items())
        try:
            hash(value)
        except TypeError:
            return type(value).__name__, repr(value)
        return value
