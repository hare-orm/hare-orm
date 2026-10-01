from decimal import Decimal, InvalidOperation
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_DECIMAL_COLLATION_NAME,
    SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME,
    SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME,
    SQLITE_DECIMAL_TEXT_FUNCTION_NAME,
    SQLITE_SORT_KEY_TEXT_CLASS,
)
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.dialects.sqlite.functions.sort_keys import SqliteSortKeys


class SqliteDecimalCollation:
    """Backs ``SQLITE_DECIMAL_COLLATION_NAME`` - orders decimal text by its exact value - and
    ``SQLITE_DECIMAL_TEXT_FUNCTION_NAME``.

    Text that isn't a number (or is NaN) sorts after every number, by its own text.
    """

    @staticmethod
    def get_decimal_text(value: int | float | str | bytes | None) -> str | None:
        """Backs ``SQLITE_DECIMAL_TEXT_FUNCTION_NAME``.

        Args:
            value: Any SQLite value.

        Returns:
            The value's exact decimal text (a double at its shortest round-tripping digits), text
            unchanged, None for None or a BLOB.
        """
        if value is None or isinstance(value, bytes):
            return None
        if isinstance(value, float):
            return repr(value)
        return str(value)

    @staticmethod
    def get_sort_key(text: str) -> tuple[int, Decimal | str]:
        """Sort key of one compared text.

        Args:
            text: The stored or bound text.

        Returns:
            ``(0, value)`` for a number, ``(1, text)`` otherwise.
        """
        try:
            value = Decimal(text)
        except InvalidOperation, ValueError:
            return 1, text
        if value.is_nan():
            return 1, text
        return 0, value

    @classmethod
    def get_sort_key_bytes(cls, value: int | float | str | bytes | None) -> bytes | None:
        """Backs ``SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME``: the byte key ordering a value as the
        collation does.

        Args:
            value: Any SQLite value.

        Returns:
            The key; None for NULL.
        """
        if value is None:
            return None
        key = SqliteSortKeys.get_non_text_key(value)
        if key is not None:
            return key
        text = str(value)
        rank, sort_value = cls.get_sort_key(text)
        if rank == 0:
            return SQLITE_SORT_KEY_TEXT_CLASS + b"\x01" + SqliteSortKeys.get_number_key(sort_value)  # type: ignore[arg-type]
        return SQLITE_SORT_KEY_TEXT_CLASS + b"\x02" + text.encode()

    @classmethod
    def compare(cls, left: str, right: str) -> int:
        """Compares two texts as exact decimals.

        Args:
            left: The left operand's text.
            right: The right operand's text.

        Returns:
            A negative number, zero or a positive number, like ``cmp()``.
        """
        left_key = cls.get_sort_key(left)
        right_key = cls.get_sort_key(right)
        return (left_key > right_key) - (left_key < right_key)

    @classmethod
    def overflows(cls, value: int | float | str | bytes | None, max_digits: int, decimal_places: int) -> int:
        """Backs ``SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME``.

        Args:
            value: Any SQLite value.
            max_digits: The digits ``DECIMAL(max_digits, decimal_places)`` holds.
            decimal_places: How many of them are fractional.

        Returns:
            1 when the value is a number with more fractional digits or more whole digits than the
            type holds, 0 otherwise - None, a BLOB and text that isn't a number included.
        """
        text = cls.get_decimal_text(value)
        if text is None:
            return 0
        try:
            number = Decimal(text)
        except InvalidOperation, ValueError:
            return 0
        if number.is_nan():
            return 0
        if number.is_infinite():
            return 1
        exponent = number.normalize().as_tuple().exponent
        fractional_digits = -exponent if isinstance(exponent, int) and exponent < 0 else 0
        return int(fractional_digits > decimal_places or abs(number) >= Decimal(10) ** (max_digits - decimal_places))

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers the collation and the decimal text and overflow functions on ``connection``.

        Args:
            connection: The SQLite connection to register the collation on.
        """
        native_functions = SqliteNativeFunctions.module
        compare: Any = cls.compare
        sort_key: Any = cls.get_sort_key_bytes
        get_decimal_text: Any = cls.get_decimal_text
        overflows: Any = cls.overflows
        if native_functions is not None:
            order = native_functions.DecimalOrder(cls.get_sort_key_bytes, cls.compare)
            compare, sort_key, get_decimal_text = order.compare, order.sort_key, order.get_decimal_text
            overflows = native_functions.DecimalOverflow(cls.overflows).overflows
        await connection._execute(  # type: ignore[no-untyped-call]
            connection._conn.create_collation, SQLITE_DECIMAL_COLLATION_NAME, compare
        )
        await connection.create_function(SQLITE_DECIMAL_TEXT_FUNCTION_NAME, 1, get_decimal_text, deterministic=True)
        await connection.create_function(SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME, 1, sort_key, deterministic=True)
        await connection.create_function(SQLITE_DECIMAL_OVERFLOWS_FUNCTION_NAME, 3, overflows, deterministic=True)
