from __future__ import annotations

from typing import Any, ClassVar

from hare.core.caching.cache import Cache


class SqliteCollation:
    """A collation hare registers on SQLite: two texts compare by their sort keys. A subclass gives the
    key of a text, and the cache of keys when making one costs more than looking it up."""

    #: (text,) -> its sort key; None when every key is made anew.
    SORT_KEYS: ClassVar[Cache[Any] | None] = None

    @classmethod
    def get_sort_key(cls, text: str) -> Any:
        """Sort key of one compared text.

        Args:
            text: The stored or bound text.

        Returns:
            The key ``make_sort_key()`` makes of it.
        """
        sort_keys = cls.SORT_KEYS
        if sort_keys is None:
            return cls.make_sort_key(text)
        sort_key = sort_keys.get((text,))
        if sort_key is None:
            sort_key = sort_keys[(text,)] = cls.make_sort_key(text)
        return sort_key

    @staticmethod
    def make_sort_key(text: str) -> Any:
        """The sort key of a text."""
        raise NotImplementedError

    @classmethod
    def compare(cls, left: str, right: str) -> int:
        """Compares two texts by their sort keys.

        Args:
            left: The left operand's text.
            right: The right operand's text.

        Returns:
            A negative number, zero or a positive number, like ``cmp()``.
        """
        left_key = cls.get_sort_key(left)
        right_key = cls.get_sort_key(right)
        return (left_key > right_key) - (left_key < right_key)
