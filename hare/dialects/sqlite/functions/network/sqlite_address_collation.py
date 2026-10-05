from __future__ import annotations

import ipaddress
from typing import ClassVar

import aiosqlite

from hare.core.caching.cache import Cache
from hare.dialects.sqlite.constants import SQLITE_ADDRESS_COLLATION_NAME
from hare.dialects.sqlite.functions.constants import SQLITE_ADDRESS_COLLATION_SORT_KEY_CACHE_SIZE


class SqliteAddressCollation:
    """Backs ``SQLITE_ADDRESS_COLLATION_NAME`` - orders stored ``IPAddressField`` text the way Postgres
    orders ``inet``: IPv4 before IPv6, then by the address's bytes; two texts of one address
    (``::1``, ``0::1``) are equal. Text that isn't an address sorts after every address, by its own
    text."""

    #: (text,) -> its sort key.
    SORT_KEYS: ClassVar[Cache[tuple[int, int, bytes, str]]] = Cache(SQLITE_ADDRESS_COLLATION_SORT_KEY_CACHE_SIZE)

    @classmethod
    def get_sort_key(cls, text: str) -> tuple[int, int, bytes, str]:
        """Sort key of one compared text.

        Args:
            text: The stored or bound text.

        Returns:
            ``(0, version, packed bytes, "")`` for an address, ``(1, 0, b"", text)`` otherwise.
        """
        sort_key = cls.SORT_KEYS.get((text,))
        if sort_key is None:
            sort_key = cls.SORT_KEYS[(text,)] = cls.make_sort_key(text)
        return sort_key

    @staticmethod
    def make_sort_key(text: str) -> tuple[int, int, bytes, str]:
        """The sort key ``get_sort_key()`` keeps for a text."""
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return 1, 0, b"", text
        return 0, address.version, address.packed, ""

    @classmethod
    def compare(cls, left: str, right: str) -> int:
        """Compares two address texts as Postgres compares ``inet`` values.

        Args:
            left: The left operand's text.
            right: The right operand's text.

        Returns:
            A negative number, zero or a positive number, like ``cmp()``.
        """
        left_key = cls.get_sort_key(left)
        right_key = cls.get_sort_key(right)
        return (left_key > right_key) - (left_key < right_key)

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers the collation on ``connection``.

        Args:
            connection: The SQLite connection to register the collation on.
        """
        await connection._execute(  # type: ignore[no-untyped-call]
            connection._conn.create_collation, SQLITE_ADDRESS_COLLATION_NAME, cls.compare
        )
