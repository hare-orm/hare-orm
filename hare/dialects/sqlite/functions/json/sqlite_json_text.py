from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.functions.constants import (
    SQLITE_JSON_PATH_TEXT_FUNCTION_NAME,
    SQLITE_JSON_TEXT_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.json.sqlite_json_ordering import SqliteJsonOrdering
from hare.dialects.sqlite.functions.json.sqlite_json_path import SqliteJsonPath


class SqliteJsonText:
    """Backs ``SQLITE_JSON_TEXT_FUNCTION_NAME`` and ``SQLITE_JSON_PATH_TEXT_FUNCTION_NAME`` - a JSON
    value as the text PostgreSQL's ``jsonb`` writes of it: an object's keys in ``jsonb``'s order, the
    last of a repeated key kept, a space after each ``,`` and ``:``, a number as its exact decimal
    text."""

    @classmethod
    def format_json(cls, value: str | bytes | float | None) -> str | None:
        """The ``jsonb`` text of a JSON value.

        Args:
            value: A JSON text, or a number a JSON path read as one.

        Returns:
            The text, the input's text unchanged if it isn't JSON, or None for None.
        """
        if value is None:
            return None
        try:
            parsed = SqliteJsonOrdering.get_json_value(value)
        except ValueError:
            return value if isinstance(value, str) else str(value)
        return cls.get_text(parsed)

    @classmethod
    def format_path_text(cls, document: str | bytes | float | None, path_json: str) -> str | None:
        """Backs ``SQLITE_JSON_PATH_TEXT_FUNCTION_NAME`` - the value at a path as the text Postgres's
        ``->>`` gives of it: a string itself, any other value its ``jsonb`` text, a JSON null NULL.

        Args:
            document: The JSON column value.
            path_json: The path as a JSON array of keys and indices.

        Returns:
            The text, or None for a missing path, a JSON null or a NULL column.
        """
        if document is None:
            return None
        try:
            parsed = SqliteJsonOrdering.get_json_value(document)
        except ValueError:
            return None
        is_found, value = SqliteJsonPath.walk(parsed, json.loads(path_json))
        if not is_found or value is None:
            return None
        return value if isinstance(value, str) else cls.get_text(value)

    @classmethod
    def get_text(cls, value: Any) -> str:
        """The ``jsonb`` text of a decoded JSON value, its numbers decoded as Decimals.

        Args:
            value: The value.

        Returns:
            The text.
        """
        if isinstance(value, dict):
            items = sorted(value.items(), key=lambda item: SqliteJsonOrdering.get_key_order(item[0]))
            members = (f"{json.dumps(key, ensure_ascii=False)}: {cls.get_text(item)}" for key, item in items)
            return "{" + ", ".join(members) + "}"
        if isinstance(value, list):
            return "[" + ", ".join(cls.get_text(item) for item in value) + "]"
        if isinstance(value, Decimal):
            # A numeric has no negative zero and no exponent.
            return format(abs(value) if value.is_zero() else value, "f")
        return json.dumps(value, ensure_ascii=False)

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``format_json`` on a SQLite connection.

        Args:
            connection: The SQLite connection.
        """
        await connection.create_function(SQLITE_JSON_TEXT_FUNCTION_NAME, 1, cls.format_json, deterministic=True)
        await connection.create_function(
            SQLITE_JSON_PATH_TEXT_FUNCTION_NAME, 2, cls.format_path_text, deterministic=True
        )
