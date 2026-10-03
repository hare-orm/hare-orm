import unicodedata
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_LOWER_FUNCTION_NAME, SQLITE_UPPER_FUNCTION_NAME
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions


class SqliteCaseMapping:
    """Backs ``Upper()``/``Lower()`` and the case-insensitive lookups - SQLite's own
    ``UPPER()``/``LOWER()`` fold ASCII only. Maps character by character like Postgres: a character
    whose mapping expands to several (``ß`` -> ``SS``) keeps its one-character mapping, or stays
    unchanged.
    """

    @staticmethod
    def upper_character(character: str) -> str:
        """The simple uppercase mapping of one character.

        Args:
            character: A single character.

        Returns:
            Its uppercase character, or ``character`` itself when it has none.
        """
        upper = character.upper()
        if len(upper) == 1:
            return upper
        # A Greek letter with iota subscript (ᾀ) uppercases to two characters but has a
        # one-character simple uppercase, the same character as its titlecase.
        title = character.title()
        return title if len(title) == 1 else character

    @staticmethod
    def lower_character(character: str) -> str:
        """The simple lowercase mapping of one character.

        Args:
            character: A single character.

        Returns:
            Its lowercase character, or ``character`` itself when it has none.
        """
        lower = character.lower()
        if len(lower) == 1:
            return lower
        # İ lowercases to i + a combining dot above; its simple lowercase is the bare i.
        if all(unicodedata.combining(mark) for mark in lower[1:]):
            return lower[0]
        return character

    @classmethod
    def upper(cls, value: str | None) -> str | None:
        """Backs ``SQLITE_UPPER_FUNCTION_NAME``.

        Args:
            value: Column value or constant passed to ``hare_upper(...)``.

        Returns:
            The upper-cased value, or ``None`` unchanged.
        """
        if value is None:
            return None
        if value.isascii():
            return value.upper()
        return "".join(map(cls.upper_character, value))

    @classmethod
    def lower(cls, value: str | None) -> str | None:
        """Backs ``SQLITE_LOWER_FUNCTION_NAME``.

        Args:
            value: Column value or constant passed to ``hare_lower(...)``.

        Returns:
            The lower-cased value, or ``None`` unchanged.
        """
        if value is None:
            return None
        if value.isascii():
            return value.lower()
        return "".join(map(cls.lower_character, value))

    @classmethod
    def get_case_variants(cls, character: str) -> set[str]:
        """The characters a case-insensitive Postgres regex matches for ``character``: itself and
        its simple uppercase and lowercase."""
        return {character, cls.upper_character(character), cls.lower_character(character)}

    @classmethod
    def get_functions(cls) -> Any:
        """The functions registered - the native ones when the extension is built, else this class."""
        native_functions = SqliteNativeFunctions.module
        if native_functions is None:
            return cls
        return native_functions.CaseMapping(cls.upper, cls.lower)

    @staticmethod
    async def install_upper(connection: aiosqlite.Connection) -> None:
        """Registers ``SqliteCaseMapping.upper`` on ``connection`` as ``SQLITE_UPPER_FUNCTION_NAME``.

        Args:
            connection: The SQLite connection.
        """
        await connection.create_function(SQLITE_UPPER_FUNCTION_NAME, 1, SqliteCaseMapping.get_functions().upper)

    @staticmethod
    async def install_lower(connection: aiosqlite.Connection) -> None:
        """Registers ``SqliteCaseMapping.lower`` on ``connection`` as ``SQLITE_LOWER_FUNCTION_NAME``.

        Args:
            connection: The SQLite connection.
        """
        await connection.create_function(SQLITE_LOWER_FUNCTION_NAME, 1, SqliteCaseMapping.get_functions().lower)
