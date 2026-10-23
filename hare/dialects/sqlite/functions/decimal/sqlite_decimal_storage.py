from __future__ import annotations

from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.functions.constants import SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME
from hare.dialects.sqlite.functions.decimal.sqlite_decimal_collation import SqliteDecimalCollation
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.parameters.constants import SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
from hare.dialects.sqlite.parameters.sqlite_parameter_adapters import SqliteParameterAdapters
from hare.fields.constants import DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION


class SqliteDecimalStorage:
    """Backs ``SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME`` - the text a ``DecimalField`` column of
    a row an UPDATE sets from an expression stores, the text a plain write of the value stores."""

    @staticmethod
    def get_stored_text(
        value: int | float | str | bytes | None, max_digits: int, decimal_places: int, value_scale: int | None
    ) -> int | float | str | bytes | None:
        """Backs ``SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME``.

        Args:
            value: The expression's value.
            max_digits: The field's ``max_digits``.
            decimal_places: The field's ``decimal_places``.
            value_scale: The places of a Decimal expression's result; None for any other one.

        Returns:
            The value quantized to ``decimal_places`` as bound text - a double of an expression of
            ``value_scale`` places first to that scale; the value itself when it isn't a number that
            quantizes - stored as SQLite takes it.
        """
        if value is None or isinstance(value, bytes):
            return value
        try:
            quant = Decimal("1" if decimal_places == 0 else f"1.{'0' * decimal_places}")
            precision = max(max_digits, DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION)
            context = Context(prec=precision, rounding=ROUND_HALF_UP)
            if isinstance(value, float) and value_scale is not None:
                # The double's exact value rounded to the expression's scale, as a read of the
                # expression decodes it.
                value_context = Context(prec=precision + value_scale, rounding=ROUND_HALF_UP)
                number = Decimal(value).quantize(Decimal(1).scaleb(-value_scale), context=value_context)
            else:
                # A double reads at its shortest round-tripping digits, as the overflow check reads it.
                text = SqliteDecimalCollation.get_decimal_text(value)
                if text is None:
                    return value
                number = Decimal(text)
            quantized_value = number.quantize(quant, context=context)
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
            SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME, 4, get_stored_text, deterministic=True
        )
