from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_TEXT_FUNCTION_PREFIX
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteTextFunctions:
    """The text UDFs ``TextFunction`` renders calls to on SQLite, with Postgres's semantics: a
    negative ``LEFT``/``RIGHT`` length drops characters from the other end, ``SUBSTR`` counts
    positions before 1 as empty, and ``LPAD``/``RPAD`` cut a longer text to the length."""

    @staticmethod
    def get_text(value: Any) -> str:
        """A value as text."""
        if isinstance(value, bytes):
            return value.decode()
        return value if isinstance(value, str) else str(value)

    @classmethod
    def left(cls, value: Any, length: Any) -> str | None:
        """``LEFT`` - a negative length keeps all but the last characters."""
        if value is None or length is None:
            return None
        return cls.get_text(value)[: int(length)]

    @classmethod
    def right(cls, value: Any, length: Any) -> str | None:
        """``RIGHT`` - a negative length keeps all but the first characters."""
        if value is None or length is None:
            return None
        text, count = cls.get_text(value), int(length)
        if count >= 0:
            return text[max(len(text) - count, 0) :] if count else ""
        return text[-count:]

    @classmethod
    def substring(cls, value: Any, position: Any, length: Any = None) -> str | None:
        """``SUBSTR(value, position[, length])`` - positions start at 1.

        Raises:
            ValueError: The length is negative.
        """
        if value is None or position is None:
            return None
        text, start = cls.get_text(value), int(position)
        if length is None:
            return text[max(start, 1) - 1 :]
        count = int(length)
        if count < 0:
            raise ValueError("negative substring length not allowed")
        end = start + count
        return text[max(start, 1) - 1 : max(end - 1, 0)]

    @classmethod
    def pad(cls, value: Any, length: Any, fill: Any, on_left: bool) -> str | None:
        """``LPAD``/``RPAD`` - a longer text is cut to ``length`` from its end."""
        if value is None or length is None or fill is None:
            return None
        text, size, fill_text = cls.get_text(value), int(length), cls.get_text(fill)
        if size <= 0:
            return ""
        if len(text) >= size or not fill_text:
            return text[:size]
        padding = (fill_text * ((size - len(text)) // len(fill_text) + 1))[: size - len(text)]
        return padding + text if on_left else text + padding

    @classmethod
    def repeat(cls, value: Any, count: Any) -> str | None:
        """``REPEAT`` - a count below 1 gives an empty text."""
        if value is None or count is None:
            return None
        return cls.get_text(value) * max(int(count), 0)

    @classmethod
    def reverse(cls, value: Any) -> str | None:
        """``REVERSE``."""
        return None if value is None else cls.get_text(value)[::-1]

    @classmethod
    def character(cls, code: Any) -> str | None:
        """``CHR``.

        Raises:
            ValueError: The code is 0 or not a character.
        """
        if code is None:
            return None
        number = int(code)
        if number == 0:
            raise ValueError("null character not permitted")
        return chr(number)

    @classmethod
    def code_point(cls, value: Any) -> int | None:
        """``ASCII`` - the first character's code point, 0 of an empty text."""
        if value is None:
            return None
        text = cls.get_text(value)
        return ord(text[0]) if text else 0

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
            "substr": (-1, cls.substring),
            "lpad": (3, lambda value, length, fill: cls.pad(value, length, fill, on_left=True)),
            "rpad": (3, lambda value, length, fill: cls.pad(value, length, fill, on_left=False)),
            "repeat": (2, cls.repeat),
            "reverse": (1, cls.reverse),
            "chr": (1, cls.character),
            "ascii": (1, cls.code_point),
            "md5": (1, cls.get_digest("md5")),
            "sha1": (1, cls.get_digest("sha1")),
            "sha224": (1, cls.get_digest("sha224")),
            "sha256": (1, cls.get_digest("sha256")),
            "sha384": (1, cls.get_digest("sha384")),
            "sha512": (1, cls.get_digest("sha512")),
        }

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers every text UDF on ``connection``."""
        functions = cls.get_functions()
        native_functions = SqliteNativeFunctions.module
        native_text = None if native_functions is None else native_functions.TextFunctions(functions)
        for name, (argument_count, function) in functions.items():
            if native_text is not None:
                function = getattr(native_text, name)
            await connection.create_function(
                f"{SQLITE_TEXT_FUNCTION_PREFIX}{name}", argument_count, function, deterministic=True
            )
