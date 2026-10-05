from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_INTEGER_MAX,
    SQLITE_INTEGER_MIN,
    SQLITE_JSON_PATH_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteJsonPath:
    """Walks a JSON path holding a digit segment - an object's key or an array's index, whichever
    the value at that point is - returning what ``json_extract()``/``json_type()`` would."""

    @staticmethod
    def get_sqlite_number(value: int | float) -> int | float:
        """A JSON number as SQLite holds it - an integer outside 64 bits becomes a REAL."""
        if isinstance(value, int) and not SQLITE_INTEGER_MIN <= value <= SQLITE_INTEGER_MAX:
            return float(value)
        return value

    @staticmethod
    def get_type_name(value: Any) -> str:
        """The ``json_type()`` name of a decoded JSON value."""
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "real"
        if isinstance(value, str):
            return "text"
        return "array" if isinstance(value, list) else "object"

    @classmethod
    def render(cls, value: Any, mode: str) -> str | int | float | None:
        """A decoded JSON value in the form ``mode`` names.

        Args:
            value: The decoded value found at the path.
            mode: ``text`` for ``json_extract()``'s unwrapped value, ``json`` for the value as
                JSON text (a number staying a number), ``type`` for its ``json_type()`` name.

        Returns:
            The rendered value.
        """
        if mode == "type":
            return cls.get_type_name(value)
        if isinstance(value, bool):
            if mode == "text":
                return int(value)
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return cls.get_sqlite_number(value)
        if value is None:
            return None if mode == "text" else "null"
        if isinstance(value, str) and mode == "text":
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def extract(cls, document: str | bytes | int | float | None, path_json: str, mode: str) -> Any:
        """Backs ``SQLITE_JSON_PATH_FUNCTION_NAME``.

        Args:
            document: The JSON column value.
            path_json: The path as a JSON array of keys and indices.
            mode: See ``render``.

        Returns:
            The rendered value at the path, or ``None`` for a missing path or a NULL column.
        """
        if document is None:
            return None
        value = document if isinstance(document, (int, float)) else json.loads(document)
        for part in json.loads(path_json):
            if isinstance(part, int) and isinstance(value, list):
                index = part if part >= 0 else len(value) + part
                if not 0 <= index < len(value):
                    return None
                value = value[index]
            elif isinstance(value, dict) and str(part) in value:
                value = value[str(part)]
            else:
                return None
        return cls.render(value, mode)

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``extract`` on ``connection`` as ``SQLITE_JSON_PATH_FUNCTION_NAME``."""
        native_functions = SqliteNativeFunctions.module
        extract: Any = cls.extract if native_functions is None else native_functions.JsonPath(cls.extract).extract
        await connection.create_function(SQLITE_JSON_PATH_FUNCTION_NAME, 3, extract, deterministic=True)
