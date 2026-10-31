from __future__ import annotations

from collections.abc import Collection
from hashlib import sha256
from typing import ClassVar

from hare.core.caching.cache import Cache
from hare.sql.constants import IDENTIFIER_DIGEST_LENGTH, IDENTIFIER_LENGTH_LIMIT, SHORTENED_IDENTIFIER_CACHE_SIZE


class Identifiers:
    """Names hare generates itself - table, column and through-table names, join and subquery
    aliases - kept within ``IDENTIFIER_LENGTH_LIMIT`` bytes, the identifier length limit of every
    dialect hare registers.

    A database truncates a longer identifier on its own (to 63 bytes, say) or rejects it,
    so two generated names that only differ past the limit would silently become one - a
    duplicate alias error, or a join against the wrong table. A name over the limit is instead
    cut to fit and suffixed with a digest of the whole name, so it stays unique and is the same
    on every run. The limit is fixed, not taken from the dialects loaded: a model is declared once
    and may run on any connection.
    """

    #: (name, limit) -> the shortened name of a name over its limit.
    SHORTENED_NAMES: ClassVar[Cache[str]] = Cache(SHORTENED_IDENTIFIER_CACHE_SIZE)

    @staticmethod
    def get_within_limit(name: str) -> str:
        """Returns ``name``, or its shortened form when it is longer than
        ``IDENTIFIER_LENGTH_LIMIT`` bytes.

        Args:
            name: The generated name.

        Returns:
            The name within the limit.
        """
        return Identifiers.shorten(name, IDENTIFIER_LENGTH_LIMIT)

    @staticmethod
    def shorten(name: str, limit: int | None) -> str:
        """Returns ``name``, or - when its UTF-8 form is longer than ``limit`` bytes - its first
        bytes followed by ``_`` and a digest of the whole name, ``limit`` bytes in all. A cut
        inside a multi-byte character drops that character.

        Args:
            name: The name.
            limit: The most bytes the name may take, None for no limit.

        Returns:
            The name within the limit.
        """
        encoded_name = name.encode()
        if limit is None or len(encoded_name) <= limit:
            return name
        key = (name, limit)
        shortened_name = Identifiers.SHORTENED_NAMES.get(key)
        if shortened_name is None:
            digest = sha256(encoded_name).hexdigest()[:IDENTIFIER_DIGEST_LENGTH]
            prefix = encoded_name[: limit - IDENTIFIER_DIGEST_LENGTH - 1].decode(errors="ignore")
            shortened_name = Identifiers.SHORTENED_NAMES[key] = f"{prefix}_{digest}"
        return shortened_name

    @staticmethod
    def get_free_name(name: str, taken_names: Collection[str]) -> str:
        """A generated name none of ``taken_names`` holds - ``name`` itself, else the first free one of
        ``<name> 2``, ``<name> 3``, ... - within ``IDENTIFIER_LENGTH_LIMIT`` bytes, and told apart from the
        taken names regardless of letter case, as a database may read identifiers.

        Args:
            name: The name.
            taken_names: The names it may not be.

        Returns:
            The free name.
        """
        candidate = Identifiers.get_within_limit(name)
        if not taken_names:
            return candidate
        folded_taken_names = {taken_name.casefold() for taken_name in taken_names}
        number = 2
        while candidate.casefold() in folded_taken_names:
            candidate = Identifiers.get_within_limit(f"{name} {number}")
            number += 1
        return candidate
