from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_JSON_HAS_KEY_FUNCTION_NAME,
    SQLITE_JSON_HAS_KEYS_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteJsonKeys:
    """Key existence tests matching Postgres jsonb ``?``/``?&``/``?|``: a key of an object, a
    string element of an array, or a string equal to the key."""

    @staticmethod
    def parse(document: str | bytes | int | float) -> Any:
        """Decodes a JSON column value, or a path value as JSON text (a number stays a number)."""
        return document if isinstance(document, (int, float)) else json.loads(document)

    @staticmethod
    def contains_key(value: Any, key: str) -> bool:
        """Whether a decoded JSON value holds ``key`` the way jsonb ``?`` tests it."""
        if isinstance(value, dict):
            return key in value
        if isinstance(value, list):
            return any(isinstance(item, str) and item == key for item in value)
        return isinstance(value, str) and value == key

    @classmethod
    def has_key(cls, document: str | bytes | int | float | None, key: str | None) -> int | None:
        """Backs ``SQLITE_JSON_HAS_KEY_FUNCTION_NAME``.

        Returns:
            1 or 0, or ``None`` for a NULL column or a missing path.
        """
        if document is None or key is None:
            return None
        return int(cls.contains_key(cls.parse(document), key))

    @classmethod
    def has_keys(cls, document: str | bytes | int | float | None, keys_json: str, mode: str) -> int | None:
        """Backs ``SQLITE_JSON_HAS_KEYS_FUNCTION_NAME``.

        Args:
            document: The JSON value tested.
            keys_json: The keys as a JSON array.
            mode: ``all`` for ``?&``, ``any`` for ``?|``.

        Returns:
            1 or 0, or ``None`` for a NULL column or a missing path.
        """
        if document is None:
            return None
        value = cls.parse(document)
        matches = (cls.contains_key(value, key) for key in json.loads(keys_json))
        return int(all(matches) if mode == "all" else any(matches))

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``has_key``/``has_keys`` on ``connection``."""
        native_functions = SqliteNativeFunctions.module
        has_key: Any = cls.has_key
        has_keys: Any = cls.has_keys
        if native_functions is not None:
            # Containment is the Python fallback's own - not reached through these two.
            keys = native_functions.JsonContainment(None, cls.has_key, cls.has_keys)
            has_key, has_keys = keys.has_key, keys.has_keys
        await connection.create_function(SQLITE_JSON_HAS_KEY_FUNCTION_NAME, 2, has_key, deterministic=True)
        await connection.create_function(SQLITE_JSON_HAS_KEYS_FUNCTION_NAME, 3, has_keys, deterministic=True)
