from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from hare.exceptions import QueryError


class CollectionArguments:
    """The checks of an argument taking a collection - of objects, of field names, of values by
    name - refusing what isn't one before it is read as one."""

    @staticmethod
    def get_list(value: Any, label: str) -> list[Any]:
        """The items of an argument taking an iterable.

        Args:
            value: The argument.
            label: The argument, as the message names it - ``"bulk_create() objects"``.

        Returns:
            The items.

        Raises:
            QueryError: ``value`` is a string, bytes, a mapping or no iterable at all.
        """
        if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
            raise QueryError(f"{label} takes a list, got {value!r}")
        return list(value)
