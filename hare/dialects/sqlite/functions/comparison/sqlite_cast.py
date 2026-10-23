from __future__ import annotations

import datetime
import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_CAST_FUNCTION_NAME
from hare.dialects.sqlite.functions.constants import INTEGER_OUT_OF_RANGE_MESSAGE, SQLITE_CAST_BOOLEAN_TEXTS
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.lazy_loading.lazy_pattern import LazyPattern
from hare.sql.enums import CastType
from hare.time import Timezone


class SqliteCast:
    """Backs ``SQLITE_CAST_FUNCTION_NAME`` - ``Cast()`` on SQLite to a number, a boolean or a date/time,
    as SQLite's own ``CAST`` converts (a fraction cut off), refusing what that would silently turn
    into another value: text that isn't a number (``CAST`` makes 0 of it), an integer past the target
    field's range (``CAST`` keeps or clamps it), NaN (SQLite stores NULL). A Decimal, kept as text,
    rounds half away from zero to its places, as a ``DecimalField`` rounds; a date/time is read from its
    stored text in UTC."""

    NUMERIC_TEXT_PATTERN = LazyPattern(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*")
    FLOAT_WORDS = frozenset({"inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"})

    @classmethod
    def get_decimal(cls, value: Any) -> Decimal:
        """A value as a Decimal - a float at its shortest round-tripping digits.

        Raises:
            ValueError: Text that isn't a number, or a float that isn't finite.
        """
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{value!r} is no decimal number")
            return Decimal(repr(value))
        text = value.decode() if isinstance(value, bytes) else str(value)
        if not cls.NUMERIC_TEXT_PATTERN.fullmatch(text):
            raise ValueError(f"{text!r} is not a number")
        return Decimal(text.strip())

    @classmethod
    def to_integer(cls, value: Any, bits: int) -> int:
        """The number with its fraction cut off.

        Raises:
            ValueError: Bad input, or an integer past the target field's ``bits`` - SQLite would keep
                one its field reads back none of.
        """
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{value!r} is no integer")
            number = int(value)
        elif isinstance(value, int):
            number = value
        else:
            number = int(cls.get_decimal(value))
        if not -(2 ** (bits - 1)) <= number < 2 ** (bits - 1):
            raise ValueError(INTEGER_OUT_OF_RANGE_MESSAGE)
        return number

    @classmethod
    def to_float(cls, value: Any) -> float:
        """Raises: ValueError: Bad input, or NaN."""
        if isinstance(value, (int, float)):
            return float(value)
        text = (value.decode() if isinstance(value, bytes) else str(value)).strip()
        if text.lower() in cls.FLOAT_WORDS:
            return float(text)
        return float(cls.get_decimal(text))

    @classmethod
    def to_decimal(cls, value: Any, max_digits: int, decimal_places: int) -> str:
        """Rounds half away from zero to ``decimal_places``.

        Raises:
            ValueError: Bad input, or more integer digits than the field holds.
        """
        try:
            number = cls.get_decimal(value).quantize(Decimal(1).scaleb(-decimal_places), rounding=ROUND_HALF_UP)
        except InvalidOperation:
            raise ValueError(f"{value!r} overflows a decimal of {max_digits} digits") from None
        integer_digits = len(str(abs(int(number)))) if abs(number) >= 1 else 0
        if integer_digits > max_digits - decimal_places:
            raise ValueError(f"{number} overflows a decimal of {max_digits} digits, {decimal_places} after the point")
        return str(number)

    @staticmethod
    def to_boolean(value: Any) -> int:
        """1 or 0 - a number by whether it is zero, text of ``true``/``false``/``1``/``0``.

        Raises:
            ValueError: Any other text.
        """
        if isinstance(value, (int, float)):
            return int(value != 0)
        text = (value.decode() if isinstance(value, bytes) else str(value)).strip().lower()
        if text not in SQLITE_CAST_BOOLEAN_TEXTS:
            raise ValueError(f"{text!r} is not a boolean")
        return SQLITE_CAST_BOOLEAN_TEXTS[text]

    @staticmethod
    def get_moment(value: Any) -> datetime.datetime:
        """A stored date/datetime text as a UTC moment - no offset means UTC."""
        moment = datetime.datetime.fromisoformat(str(value).strip())
        if moment.tzinfo is None:
            return moment.replace(tzinfo=datetime.UTC)
        return moment.astimezone(datetime.UTC)

    @staticmethod
    def get_wall_clock(moment: datetime.datetime) -> datetime.datetime:
        """A naive timestamp's wall clock - an aware one in the machine's local zone."""
        return Timezone.get_system_local_naive(moment) if moment.tzinfo is not None else moment

    @classmethod
    def get_naive_moment(cls, value: Any, source: str) -> datetime.datetime:
        """A date/datetime text as a naive wall clock - a text's own offset ignored."""
        moment = datetime.datetime.fromisoformat(str(value).strip())
        if source == CastType.DATETIME:
            return cls.get_wall_clock(moment)
        return moment.replace(tzinfo=None)

    @classmethod
    def to_temporal(cls, value: Any, source: str, target: str, is_aware: bool = True) -> str:
        """A date, datetime or time of day, read in UTC - a naive timestamp as its wall clock.

        Raises:
            ValueError: A number, or text that isn't a date/time.
        """
        if isinstance(value, (int, float)):
            raise ValueError(f"{value!r} is no {target}")
        text = str(value).strip()
        if target == CastType.TIME:
            if source == CastType.DATETIME:
                moment = cls.get_moment(text) if is_aware else cls.get_naive_moment(text, source)
                return moment.time().isoformat()
            try:
                return datetime.time.fromisoformat(text).replace(tzinfo=None).isoformat()
            except ValueError:
                pass
            # A timestamp's text gives its own clock digits, whatever its offset; a date its midnight.
            return datetime.datetime.fromisoformat(text).time().replace(tzinfo=None).isoformat()
        if target == CastType.DATE and source != CastType.DATETIME:
            # A text's date as it is written, whatever time and offset follow it.
            return datetime.datetime.fromisoformat(text).date().isoformat()
        moment = cls.get_moment(text) if is_aware else cls.get_naive_moment(text, source)
        if target == CastType.DATE:
            return moment.date().isoformat()
        return moment.isoformat(" ")

    @classmethod
    def cast(
        cls, value: Any, target: str, source: str, first_parameter: Any, second_parameter: Any, is_aware: int
    ) -> Any:
        """Backs ``SQLITE_CAST_FUNCTION_NAME``.

        Args:
            value: The value.
            target: The ``CastType`` converted to - not text, which SQLite's own ``CAST`` writes.
            source: The ``CastType`` of the value, or ``unknown``.
            first_parameter: An integer target's bits, a Decimal's max digits.
            second_parameter: A Decimal's decimal places.
            is_aware: Whether timestamps are aware - a naive one converts as its wall clock.

        Returns:
            The converted value, or ``None`` for NULL.

        Raises:
            ValueError: The value can't be converted.
        """
        if value is None:
            return None
        if target == CastType.INTEGER:
            return cls.to_integer(value, int(first_parameter))
        if target == CastType.FLOAT:
            number = cls.to_float(value)
            if math.isnan(number):
                raise ValueError("NaN, which SQLite stores as NULL")
            return number
        if target == CastType.DECIMAL:
            return cls.to_decimal(value, int(first_parameter), int(second_parameter))
        if target == CastType.BOOLEAN:
            return cls.to_boolean(value)
        return cls.to_temporal(value, source, target, bool(is_aware))

    @classmethod
    async def install(cls, connection: aiosqlite.Connection, function_errors: SqliteFunctionErrors) -> None:
        """Registers ``cast`` on ``connection`` as ``SQLITE_CAST_FUNCTION_NAME``.

        Args:
            connection: The SQLite connection.
            function_errors: The connection's function errors.
        """
        native_functions = SqliteNativeFunctions.module
        cast: Any = function_errors.wrap(cls.cast)
        if native_functions is not None:
            cast = native_functions.SqliteCast(cast).cast
        await connection.create_function(SQLITE_CAST_FUNCTION_NAME, 6, cast, deterministic=True)
