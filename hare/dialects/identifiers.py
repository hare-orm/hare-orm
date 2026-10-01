from __future__ import annotations

from hashlib import sha256
from typing import ClassVar

from hare.core.cache import Cache
from hare.dialects.constants import (
    IDENTIFIER_DIGEST_LENGTH,
    IDENTIFIER_LENGTH_LIMIT,
    SHORTENED_IDENTIFIER_CACHE_SIZE,
)


class Identifiers:
    """Names hare generates itself - table, column and through-table names, join and subquery
    aliases - kept within ``IDENTIFIER_LENGTH_LIMIT`` bytes, the identifier length limit of every
    dialect hare registers.

    A database truncates a longer identifier on its own (PostgreSQL to 63 bytes) or rejects it,
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
