from __future__ import annotations

from decimal import Decimal
from functools import partial
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_GREATEST_FUNCTION_NAME,
    SQLITE_LEAST_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions


class SqliteGreatestLeast:
    """Backs ``SQLITE_GREATEST_FUNCTION_NAME``/``SQLITE_LEAST_FUNCTION_NAME`` - SQLite's multi-argument
    ``max()``/``min()``, NULL when any argument is NULL, with numbers compared as numbers, a
    DecimalField's stored text included."""

    @staticmethod
    def get_sort_key(value: Any, comparison_type: str) -> Any:
        """What an argument is compared by - a Decimal for a number."""
        if comparison_type == "number":
            return (
                value
                if isinstance(value, (int, float))
                else Decimal(value.decode() if isinstance(value, bytes) else value)
            )
        return value

    @classmethod
    def pick(cls, is_greatest: bool, comparison_type: str, *values: Any) -> Any:
        """The greatest/least argument, NULL when any one is.

        Args:
            is_greatest: Pick the greatest, else the least.
            comparison_type: ``number`` to compare numerically, anything else as stored.
            values: The arguments.

        Returns:
            The picked argument as it was passed - a float of a number picked among floats, the type
            the result of a float and a Decimal is.
        """
        if any(value is None for value in values):
            return None
        choose = max if is_greatest else min
        picked = choose(values, key=lambda value: cls.get_sort_key(value, comparison_type))
        if comparison_type == "number" and any(isinstance(value, float) for value in values):
            return float(cls.get_sort_key(picked, comparison_type))
        return picked

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers both functions on ``connection``."""
        native_functions = SqliteNativeFunctions.module
        greatest: Any = partial(cls.pick, True)
        least: Any = partial(cls.pick, False)
        if native_functions is not None:
            functions = native_functions.GreatestLeast(cls.pick)
            greatest, least = functions.greatest, functions.least
        await connection.create_function(SQLITE_GREATEST_FUNCTION_NAME, -1, greatest, deterministic=True)
        await connection.create_function(SQLITE_LEAST_FUNCTION_NAME, -1, least, deterministic=True)
