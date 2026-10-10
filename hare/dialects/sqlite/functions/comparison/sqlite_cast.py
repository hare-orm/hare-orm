from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_CAST_FUNCTION_NAME
from hare.dialects.sqlite.functions.constants import BOOLEAN_CAST_INTEGER_BITS, DATE_TEXT_LENGTH
from hare.dialects.sqlite.functions.sqlite_native_functions import SqliteNativeFunctions
from hare.lazy_loading.lazy_pattern import LazyPattern
from hare.sql.enums import CastType
from hare.sql.functions.text.number_text import NumberText


class SqliteCast:
    """Backs ``SQLITE_CAST_FUNCTION_NAME`` - SQLite's own ``CAST`` truncates fractions, turns bad
    text into 0 and has no date types, so ``Cast()`` converts with Postgres's rules instead: a
    float rounds half to even and a numeric half away from zero, bad input raises, and a date/time
    is read in UTC, the Postgres session zone."""

    INTEGER_TEXT_PATTERN = LazyPattern(r"\s*[+-]?\d+\s*")
    NUMERIC_TEXT_PATTERN = LazyPattern(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*")
    FLOAT_WORDS = frozenset({"nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"})

    @staticmethod
    def get_float_decimal(value: float) -> Decimal:
        """A float as the numeric Postgres converts it to - 15 significant digits."""
        return Decimal(format(value, ".15g"))

    @classmethod
    def get_decimal(cls, value: Any, source: str) -> Decimal:
        """A value as a Decimal.

        Raises:
            ValueError: Text that isn't a number, or a value of another type.
        """
        if isinstance(value, bool) or source == CastType.BOOLEAN:
            raise ValueError("cannot cast boolean to a number")
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            return cls.get_float_decimal(value)
        text = value.decode() if isinstance(value, bytes) else str(value)
        if source in {CastType.DATE, CastType.DATETIME, CastType.TIME}:
            raise ValueError(f"cannot cast a {source} to a number")
        if not cls.NUMERIC_TEXT_PATTERN.fullmatch(text):
            raise ValueError(f"invalid input syntax for type numeric or a NaN SQLite can't store: {text!r}")
        return Decimal(text.strip())

    @classmethod
    def to_integer(cls, value: Any, source: str, bits: int) -> int:
        """Rounds like Postgres - a float half to even, a numeric half away from zero.

        Raises:
            ValueError: Bad input, or a value out of the target's range.
        """
        if source == CastType.BOOLEAN:
            if bits != BOOLEAN_CAST_INTEGER_BITS:
                raise ValueError("cannot cast boolean to an integer other than a 32-bit one")
            number = int(value)
        elif isinstance(value, (str, bytes)) and source in {CastType.TEXT, CastType.UNKNOWN}:
            text = value.decode() if isinstance(value, bytes) else value
            if not cls.INTEGER_TEXT_PATTERN.fullmatch(text):
                raise ValueError(f"invalid input syntax for type integer: {text!r}")
            number = int(text.strip())
        elif isinstance(value, float):
            number = round(value)
        elif isinstance(value, int):
            number = value
        else:
            number = int(cls.get_decimal(value, source).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if not -(2 ** (bits - 1)) <= number < 2 ** (bits - 1):
            raise ValueError("integer out of range")
        return number

    @classmethod
    def to_float(cls, value: Any, source: str) -> float:
        """Raises: ValueError: Bad input."""
        if source == CastType.BOOLEAN:
            raise ValueError("cannot cast boolean to double precision")
        if isinstance(value, (int, float)):
            return float(value)
        text = (value.decode() if isinstance(value, bytes) else str(value)).strip()
        if source in {CastType.TEXT, CastType.UNKNOWN} and text.lower() in cls.FLOAT_WORDS:
            number = float(text)
            if number != number:
                raise ValueError("NaN can't be stored on SQLite")
            return number
        return float(cls.get_decimal(text, source))

    @classmethod
    def to_decimal(cls, value: Any, source: str, max_digits: int, decimal_places: int) -> str:
        """Rounds half away from zero to ``decimal_places``.

        Raises:
            ValueError: Bad input, or more integer digits than the field holds.
        """
        number = cls.get_decimal(value, source).quantize(Decimal(1).scaleb(-decimal_places), rounding=ROUND_HALF_UP)
        integer_digits = len(str(abs(int(number)))) if abs(number) >= 1 else 0
        if integer_digits > max_digits - decimal_places:
            raise ValueError("numeric field overflow")
        return str(number)

    @staticmethod
    def trim_fraction(text: str) -> str:
        """Fractional seconds without trailing zeros, as Postgres prints them - ``.5``."""
        if "." not in text:
            return text
        return text.rstrip("0").rstrip(".")

    @staticmethod
    def format_offset(offset: datetime.timedelta | None) -> str:
        """A UTC offset as Postgres prints it - ``+03``, ``+05:30``."""
        if offset is None:
            return ""
        total_minutes = int(offset.total_seconds()) // 60
        sign = "-" if total_minutes < 0 else "+"
        hours, minutes = divmod(abs(total_minutes), 60)
        return f"{sign}{hours:02d}" + (f":{minutes:02d}" if minutes else "")

    @classmethod
    def to_text(cls, value: Any, source: str, max_length: int | None, is_aware: bool = True) -> str:
        """The value's Postgres text, cut to ``max_length`` - a naive timestamp as its wall clock."""
        if source == CastType.BOOLEAN or isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, float):
            text = NumberText.format_float(value)
        elif source == CastType.DATETIME:
            moment = datetime.datetime.fromisoformat(str(value))
            if not is_aware:
                moment = cls.get_wall_clock(moment)
            elif moment.tzinfo is not None:
                moment = moment.astimezone(datetime.UTC)
            text = cls.trim_fraction(moment.replace(tzinfo=None).isoformat(" ")) + cls.format_offset(
                moment.utcoffset()
            )
        elif source == CastType.TIME:
            clock = datetime.time.fromisoformat(str(value))
            text = cls.trim_fraction(clock.replace(tzinfo=None).isoformat()) + cls.format_offset(clock.utcoffset())
        else:
            text = value.decode() if isinstance(value, bytes) else str(value)
        return text if max_length is None else text[:max_length]

    @staticmethod
    def to_boolean(value: Any, source: str) -> int:
        """Postgres's boolean input - ``t``/``true``/``yes``/``on``/``1`` and their prefixes.

        Raises:
            ValueError: Anything else, or a float/numeric source.
        """
        if isinstance(value, int) and not isinstance(value, bool) and source != CastType.TEXT:
            return int(value != 0)
        if source in {CastType.FLOAT, CastType.DECIMAL}:
            raise ValueError(f"cannot cast {source} to boolean")
        text = (value.decode() if isinstance(value, bytes) else str(value)).strip().lower()
        if text and ("true".startswith(text) or "yes".startswith(text) or text in {"on", "1"}):
            return 1
        if text and ("false".startswith(text) or "no".startswith(text) or text in {"off", "0"}):
            return 0
        raise ValueError(f"invalid input syntax for type boolean: {text!r}")

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
        return moment.astimezone().replace(tzinfo=None) if moment.tzinfo is not None else moment

    @classmethod
    def get_naive_moment(cls, value: Any, source: str) -> datetime.datetime:
        """A date/datetime text as a naive wall clock - a text's own offset ignored, as Postgres's
        ``timestamp`` input does."""
        moment = datetime.datetime.fromisoformat(str(value).strip())
        if source == CastType.DATETIME:
            return cls.get_wall_clock(moment)
        return moment.replace(tzinfo=None)

    @classmethod
    def to_temporal(cls, value: Any, source: str, target: str, is_aware: bool = True) -> str:
        """A date, datetime or time of day, read in UTC - a naive timestamp as its wall clock.

        Raises:
            ValueError: A number, or text that isn't one.
        """
        if isinstance(value, (int, float)) or source in {CastType.INTEGER, CastType.FLOAT, CastType.DECIMAL}:
            raise ValueError(f"cannot cast a number to {target}")
        text = str(value).strip()
        if target == CastType.TIME:
            if source == CastType.DATE:
                raise ValueError("cannot cast a date to a time")
            if source == CastType.DATETIME:
                moment = cls.get_moment(text) if is_aware else cls.get_naive_moment(text, source)
                return moment.time().isoformat()
            try:
                return datetime.time.fromisoformat(text).replace(tzinfo=None).isoformat()
            except ValueError:
                pass
            # A timestamp's text gives its own clock digits, whatever its offset; a bare date has none.
            if len(text) <= DATE_TEXT_LENGTH:
                raise ValueError(f"invalid input syntax for type time: {text!r}") from None
            return datetime.datetime.fromisoformat(text).time().replace(tzinfo=None).isoformat()
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
            target: The ``CastType`` converted to.
            source: The ``CastType`` of the value, or ``unknown``.
            first_parameter: An integer target's bits, a Decimal's max digits, a text's max length.
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
            return cls.to_integer(value, source, int(first_parameter))
        if target == CastType.FLOAT:
            return cls.to_float(value, source)
        if target == CastType.DECIMAL:
            return cls.to_decimal(value, source, int(first_parameter), int(second_parameter))
        if target == CastType.TEXT:
            max_length = None if first_parameter is None else int(first_parameter)
            return cls.to_text(value, source, max_length, bool(is_aware))
        if target == CastType.BOOLEAN:
            return cls.to_boolean(value, source)
        return cls.to_temporal(value, source, target, bool(is_aware))

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers ``cast`` on ``connection`` as ``SQLITE_CAST_FUNCTION_NAME``."""
        native_functions = SqliteNativeFunctions.module
        cast: Any = cls.cast if native_functions is None else native_functions.SqliteCast(cls.cast).cast
        await connection.create_function(SQLITE_CAST_FUNCTION_NAME, 6, cast, deterministic=True)
