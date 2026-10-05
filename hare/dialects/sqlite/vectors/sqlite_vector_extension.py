from __future__ import annotations

import json
import math
import sqlite3
from array import array
from collections.abc import Sequence
from typing import Any, ClassVar

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_VECTOR_EXTENSION_OPTION,
    SQLITE_VECTOR_NEGATIVE_INNER_PRODUCT_FUNCTION_NAME,
)
from hare.dialects.sqlite.vectors.constants import (
    SQLITE_VECTOR_ARRAY_TYPECODE,
    SQLITE_VECTOR_DIMENSION_MISMATCH_MESSAGE,
    SQLITE_VECTOR_EXTENSION_PACKAGE,
)
from hare.exceptions import ConfigurationError

try:
    import sqlite_vec
except ImportError:  # pragma: nocoverage
    sqlite_vec = None


class SqliteVectorExtension:
    """The sqlite-vec extension on a SQLite connection - its vector distance functions back
    ``VectorField``'s distances - and hare's negative inner product, which sqlite-vec has none of."""

    #: The sqlite-vec package (``hare-orm[sqlite-vec]``), None when it isn't installed.
    sqlite_vec_module: ClassVar[Any] = sqlite_vec

    @classmethod
    def raise_if_unavailable(cls) -> None:
        """Rejects ``load_sqlite_vec=True`` where the extension can't be loaded.

        Raises:
            ConfigurationError: The sqlite-vec package isn't installed, or this Python's sqlite3
                module can't load extensions.
        """
        option_name = SQLITE_VECTOR_EXTENSION_OPTION.name
        if cls.sqlite_vec_module is None:
            raise ConfigurationError(
                f"{option_name}=True needs the sqlite-vec package - install {SQLITE_VECTOR_EXTENSION_PACKAGE}"
            )
        if not hasattr(sqlite3.Connection, "enable_load_extension"):
            raise ConfigurationError(
                f"{option_name}=True needs a Python whose sqlite3 module loads extensions - this one was built "
                "without them (SQLITE_OMIT_LOAD_EXTENSION)"
            )

    @classmethod
    def load(cls, raw_connection: sqlite3.Connection) -> None:
        """Loads sqlite-vec into a connection; extension loading is switched off again after it.

        Args:
            raw_connection: The sqlite3 connection.
        """
        raw_connection.enable_load_extension(True)
        try:
            raw_connection.load_extension(cls.sqlite_vec_module.loadable_path())
        finally:
            raw_connection.enable_load_extension(False)

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Loads sqlite-vec into ``connection`` and registers the negative inner product on it."""
        await connection._execute(cls.load, connection._conn)  # type: ignore[no-untyped-call]
        await connection.create_function(
            SQLITE_VECTOR_NEGATIVE_INNER_PRODUCT_FUNCTION_NAME, 2, cls.get_negative_inner_product, deterministic=True
        )

    @staticmethod
    def get_elements(value: bytes | str) -> Sequence[float]:
        """The elements of a stored vector - sqlite-vec's float32 blob, or JSON text.

        Args:
            value: The vector.

        Returns:
            Its elements.
        """
        if isinstance(value, str):
            return [float(element) for element in json.loads(value)]
        return array(SQLITE_VECTOR_ARRAY_TYPECODE, value)

    @classmethod
    def get_negative_inner_product(cls, first: bytes | str | None, second: bytes | str | None) -> float | None:
        """Backs ``SQLITE_VECTOR_NEGATIVE_INNER_PRODUCT_FUNCTION_NAME`` - pgvector's ``<#>``.

        Args:
            first: A vector.
            second: Another vector.

        Returns:
            The inner product of the two, negated; None when either is NULL.

        Raises:
            ValueError: The vectors have different lengths.
        """
        if first is None or second is None:
            return None
        first_elements = cls.get_elements(first)
        second_elements = cls.get_elements(second)
        if len(first_elements) != len(second_elements):
            raise ValueError(
                SQLITE_VECTOR_DIMENSION_MISMATCH_MESSAGE.format(first=len(first_elements), second=len(second_elements))
            )
        return -math.sumprod(first_elements, second_elements)
