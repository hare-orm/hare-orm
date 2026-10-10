from __future__ import annotations

from decimal import Context, Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
from hare.dialects.sqlite.functions.constants import SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters
from hare.fields.constants import DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION


class SqliteDecimalStorage:
    """Backs ``SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME`` - the text a ``DecimalField`` column of
    a row an UPDATE sets from an expression stores, the text a plain write of the value stores."""

    @staticmethod
    def get_stored_text(
        value: int | float | str | bytes | None, max_digits: int, decimal_places: int
    ) -> int | float | str | bytes | None:
        """Backs ``SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME``.

        Args:
            value: The expression's value.
            max_digits: The field's ``max_digits``.
            decimal_places: The field's ``decimal_places``.

        Returns:
            The value quantized to ``decimal_places`` as bound text; the value itself when it isn't
            a number that quantizes - the written value's check rejects it.
        """
        if value is None or isinstance(value, bytes):
            return value
        try:
            quant = Decimal("1" if decimal_places == 0 else f"1.{'0' * decimal_places}")
            context = Context(prec=max(max_digits, DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION))
            quantized_value = Decimal(value).quantize(quant, context=context)
        except (ArithmeticError, ValueError, TypeError):
            return value
        if quantized_value.is_zero():
            # A numeric column has no negative zero.
            quantized_value = quantized_value.copy_abs()
        return SqliteParameterAdapters.adapt_decimal(quantized_value)

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers the function on ``connection``.

        Args:
            connection: The SQLite connection.
        """
        native_functions = SqliteNativeFunctions.module
        get_stored_text: Any = cls.get_stored_text
        if native_functions is not None:
            get_stored_text = native_functions.DecimalStoredText(
                cls.get_stored_text, DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION, SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
            ).get_stored_text
        await connection.create_function(
            SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME, 3, get_stored_text, deterministic=True
        )
