from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_TEXT_FUNCTION_PREFIX
from hare.dialects.sqlite.functions.constants import (
    SQLITE_MAX_VALUE_BYTES,
    SQLITE_TOO_BIG_MESSAGE,
    UTF8_LONGEST_CHARACTER_BYTES,
)
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteTextFunctions:
    """The text UDFs ``TextFunction`` renders calls to on SQLite, which has no such functions: a
    negative ``LEFT``/``RIGHT`` length drops characters from the other end, and ``LPAD``/``RPAD`` cut a
    longer text to the length. A text longer than SQLite holds in one value is refused before it is
    built."""

    @staticmethod
    def get_text(value: Any) -> str:
        """A value as text."""
        if isinstance(value, bytes):
            return value.decode()
        return value if isinstance(value, str) else str(value)

    @staticmethod
    def get_integer(value: Any) -> int:
        """A length or a count as an integer."""
        return int(value)

    @staticmethod
    def raise_if_too_long(byte_count: int) -> None:
        """Refuses a text of more bytes than SQLite holds in one value - before it is built.

        Raises:
            ValueError: The text is too long.
        """
        if byte_count > SQLITE_MAX_VALUE_BYTES:
            raise ValueError(SQLITE_TOO_BIG_MESSAGE)

    @classmethod
    def left(cls, value: Any, length: Any) -> str | None:
        """``LEFT`` - a negative length keeps all but the last characters."""
        if value is None or length is None:
            return None
        return cls.get_text(value)[: cls.get_integer(length)]

    @classmethod
    def right(cls, value: Any, length: Any) -> str | None:
        """``RIGHT`` - a negative length keeps all but the first characters."""
        if value is None or length is None:
            return None
        text, count = cls.get_text(value), cls.get_integer(length)
        if count >= 0:
            return text[max(len(text) - count, 0) :] if count else ""
        return text[-count:]

    @classmethod
    def pad(cls, value: Any, length: Any, fill: Any, on_left: bool) -> str | None:
        """``LPAD``/``RPAD`` - a longer text is cut to ``length`` from its end."""
        if value is None or length is None or fill is None:
            return None
        text, size, fill_text = cls.get_text(value), cls.get_integer(length), cls.get_text(fill)
        if size <= 0:
            return ""
        cls.raise_if_too_long(size * UTF8_LONGEST_CHARACTER_BYTES)
        if len(text) >= size or not fill_text:
            return text[:size]
        padding = (fill_text * ((size - len(text)) // len(fill_text) + 1))[: size - len(text)]
        return padding + text if on_left else text + padding

    @classmethod
    def repeat(cls, value: Any, count: Any) -> str | None:
        """``REPEAT`` - a count below 1 gives an empty text."""
        if value is None or count is None:
            return None
        text, times = cls.get_text(value), max(cls.get_integer(count), 0)
        cls.raise_if_too_long(len(text.encode()) * times)
        return text * times

    @classmethod
    def reverse(cls, value: Any) -> str | None:
        """``REVERSE``."""
        return None if value is None else cls.get_text(value)[::-1]

    @classmethod
    def get_digest(cls, algorithm: str) -> Callable[[Any], str | None]:
        """A hex digest of the UTF-8 text."""

        def digest(value: Any) -> str | None:
            if value is None:
                return None
            return hashlib.new(algorithm, cls.get_text(value).encode()).hexdigest()

        return digest

    @classmethod
    def get_functions(cls) -> dict[str, tuple[int, Callable[..., Any]]]:
        """Every UDF by its name after the prefix, with its argument count (-1 for any)."""
        return {
            "left": (2, cls.left),
            "right": (2, cls.right),
            "lpad": (3, lambda value, length, fill: cls.pad(value, length, fill, on_left=True)),
            "rpad": (3, lambda value, length, fill: cls.pad(value, length, fill, on_left=False)),
            "repeat": (2, cls.repeat),
            "reverse": (1, cls.reverse),
            "md5": (1, cls.get_digest("md5")),
            "sha1": (1, cls.get_digest("sha1")),
            "sha224": (1, cls.get_digest("sha224")),
            "sha256": (1, cls.get_digest("sha256")),
            "sha384": (1, cls.get_digest("sha384")),
            "sha512": (1, cls.get_digest("sha512")),
        }

    @classmethod
    async def install(cls, connection: aiosqlite.Connection, function_errors: SqliteFunctionErrors) -> None:
        """Registers every text UDF on ``connection``.

        Args:
            connection: The SQLite connection.
            function_errors: The connection's function errors.
        """
        functions = {
            name: (argument_count, function_errors.wrap(function))
            for name, (argument_count, function) in cls.get_functions().items()
        }
        native_functions = SqliteNativeFunctions.module
        native_text = None if native_functions is None else native_functions.TextFunctions(functions)
        for name, (argument_count, function) in functions.items():
            if native_text is not None:
                function = getattr(native_text, name)
            await connection.create_function(
                f"{SQLITE_TEXT_FUNCTION_PREFIX}{name}", argument_count, function, deterministic=True
            )
