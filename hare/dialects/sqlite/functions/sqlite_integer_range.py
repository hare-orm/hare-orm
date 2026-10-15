from __future__ import annotations

from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_INTEGER_IN_RANGE_FUNCTION_NAME
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors


class SqliteIntegerRange:
    """Backs ``SQLITE_INTEGER_IN_RANGE_FUNCTION_NAME`` - an integer result of arithmetic refused out of
    the range of its column type, as PostgreSQL refuses ``integer out of range``: SQLite keeps any
    64-bit integer and turns a larger one into a REAL."""

    @staticmethod
    def check(value: Any, minimum: int, maximum: int, type_name: str) -> Any:
        """The value, when it is an integer of the range.

        Args:
            value: The result.
            minimum: The smallest value of the type.
            maximum: The largest value of the type.
            type_name: The type, for the message.

        Returns:
            The value; NULL for NULL.

        Raises:
            ValueError: The value is out of the range, or a REAL an overflow turned it into - ``sqlite3``
                reports an ``OverflowError`` as a too long text.
        """
        if value is None:
            return None
        if isinstance(value, float) or not minimum <= value <= maximum:
            raise ValueError(f"{type_name} out of range")
        return value

    @classmethod
    async def install(cls, connection: aiosqlite.Connection, function_errors: SqliteFunctionErrors) -> None:
        """Registers the UDF on ``connection``.

        Args:
            connection: The SQLite connection.
            function_errors: The connection's function errors.
        """
        await connection.create_function(
            SQLITE_INTEGER_IN_RANGE_FUNCTION_NAME, 4, function_errors.wrap(cls.check), deterministic=True
        )
