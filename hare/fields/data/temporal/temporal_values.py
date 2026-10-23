from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any, TypeVar

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.constants import END_OF_DAY_HOUR_TEXT, ISO_FULL_DATE_PREFIX_PATTERN
from hare.fields.field import Field

T = TypeVar("T")


class TemporalValues:
    """What the date, time and datetime fields share converting their values."""

    @staticmethod
    def parse_text(
        field: Field[Any],
        value: str,
        parser: Callable[[str], T],
        catch: type[Exception] | tuple[type[Exception], ...] = Exception,
    ) -> T:
        """Parses a text given to a date, datetime or time field.

        Args:
            field: The field the text is given to.
            value: The text.
            parser: Reads the text into the field's value.
            catch: The errors of the parser that mean the text is no value.

        Returns:
            What the parser gives.

        Raises:
            ValidationError: The parser raised one of ``catch``.
        """
        try:
            return parser(value)
        except catch as error:
            validation_error = field.get_validation_error(error, value)
        raise validation_error

    @staticmethod
    def parse_time_of_day_text(field: Field[Any], value: str) -> datetime.time:
        """An ISO 8601 text of a time of day, parsed.

        Args:
            field: The field the value is given to.
            value: The text.

        Returns:
            The time.

        Raises:
            ValidationError: The text is no time of day - 24:00 is the end of a day, which Python 3.14
                reads as midnight at its start.
        """
        if value.strip().startswith(END_OF_DAY_HOUR_TEXT):
            raise ValidationError(
                f"{field.model_field_name}: {field.get_value_for_message(value)} is the end of a day - no time "
                "of day holds it"
            )
        return TemporalValues.parse_text(field, value, datetime.time.fromisoformat, catch=ValueError)

    @staticmethod
    def raise_if_not_full_date(field: Field[Any], value: str) -> None:
        """Rejects a DateField/DatetimeField string that doesn't start with a full calendar date
        (``2024``, ``2024-05``) - ISO 8601's reduced forms name a period, not a day.

        Args:
            field: The field the value is written to.
            value: The string.

        Raises:
            ValidationError: The string has no full ``YYYY-MM-DD``/``YYYYMMDD`` date.
        """
        if ISO_FULL_DATE_PREFIX_PATTERN.match(value) is None:
            raise ValidationError(
                f"{field.model_field_name}: {field.get_value_for_message(value)} is not an ISO 8601 date "
                "(expected YYYY-MM-DD)"
            )

    @staticmethod
    def check_auto_now_options(auto_now: bool, auto_now_add: bool) -> None:
        """Refuses ``auto_now`` and ``auto_now_add`` given together.

        Args:
            auto_now: Whether every save writes the current time.
            auto_now_add: Whether the first save writes it.

        Raises:
            ConfigurationError: Both are set.
        """
        if auto_now_add and auto_now:
            raise ConfigurationError("You can choose only 'auto_now' or 'auto_now_add'")

    @staticmethod
    def parse_iso_datetime(value: str) -> datetime.datetime:
        """Parses ISO 8601 datetime text where ciso8601 isn't installed: ``datetime.fromisoformat()``,
        much faster; the forms only the iso8601 package reads - a year alone, a year and a month -
        go to it.

        Args:
            value: The text.

        Returns:
            The datetime - naive when the text has no offset.

        Raises:
            ValueError: Neither parser reads the text.
        """
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            # Imported for the rare text fromisoformat() doesn't read, not by every program.
            from iso8601 import parse_date

            return parse_date(value, default_timezone=None)
