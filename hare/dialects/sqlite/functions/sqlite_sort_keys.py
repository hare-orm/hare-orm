from __future__ import annotations

from decimal import Decimal
from typing import Any

from hare.dialects.sqlite.functions.constants import SQLITE_SORT_KEY_BLOB_CLASS, SQLITE_SORT_KEY_NUMBER_CLASS


class SqliteSortKeys:
    """The parts of the byte keys the SQLite sort key functions build - a value's storage class
    first, as SQLite orders values of different classes, then the class's own key. One key sorts
    before another (bytewise) exactly when its value sorts first; equal values have equal keys.
    ``rust.native.sqlite_functions`` builds the same bytes."""

    @staticmethod
    def get_number_key(number: Decimal) -> bytes:
        """The key of a number - no key is the start of another.

        Args:
            number: A number, possibly infinite, not NaN.

        Returns:
            The key.
        """
        if number.is_infinite():
            return b"\x82" if number > 0 else b"\x7e"
        if number.is_zero():
            return b"\x80"
        sign, digit_tuple, exponent = number.as_tuple()
        digits = "".join(map(str, digit_tuple)).lstrip("0")
        stripped_digits = digits.rstrip("0")
        exponent = int(exponent) + len(digits) - len(stripped_digits)
        first_exponent = exponent + len(stripped_digits) - 1
        exponent_bytes = ((first_exponent % (1 << 64)) ^ (1 << 63)).to_bytes(8, "big")
        digit_bytes = stripped_digits.encode()
        if not sign:
            return b"\x81" + exponent_bytes + digit_bytes + b"\x00"
        # Every byte inverted orders the larger magnitude first; 0xFF ends the digits above any
        # inverted digit, so a shorter prefix sorts later.
        return b"\x7f" + bytes(255 - byte for byte in exponent_bytes + digit_bytes) + b"\xff"

    @staticmethod
    def get_non_text_key(value: Any) -> bytes | None:
        """The key of an INTEGER, REAL or BLOB value - None for TEXT.

        Args:
            value: A value SQLite passed to a function, not NULL.

        Returns:
            The key, or None for a str.
        """
        if isinstance(value, bytes):
            return SQLITE_SORT_KEY_BLOB_CLASS + value
        if isinstance(value, int):
            return SQLITE_SORT_KEY_NUMBER_CLASS + SqliteSortKeys.get_number_key(Decimal(value))
        if isinstance(value, float):
            return SQLITE_SORT_KEY_NUMBER_CLASS + SqliteSortKeys.get_number_key(Decimal(repr(value)))
        return None
