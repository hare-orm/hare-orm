from __future__ import annotations

import decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_NUMBER_TEXT_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.sql.functions.text.number_text import NumberText


class SqliteNumberText:
    """Backs ``SQLITE_NUMBER_TEXT_FUNCTION_NAME`` - numbers concatenated as the same text Postgres
    gives them."""

    @staticmethod
    async def install(connection: aiosqlite.Connection) -> None:
        """Registers ``NumberText.format_number`` on ``connection``.

        Args:
            connection: The SQLite connection to register the function on.
        """
        native_functions = SqliteNativeFunctions.module
        format_number: Any = NumberText.format_number
        if native_functions is not None:
            # A connection's thread starts with the default context.
            format_number = native_functions.NumberText(
                NumberText.format_number, decimal.DefaultContext.prec
            ).format_number
        await connection.create_function(SQLITE_NUMBER_TEXT_FUNCTION_NAME, 2, format_number, deterministic=True)
