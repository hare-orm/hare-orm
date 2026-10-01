from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.base.field import Field
from hare.fields.constants import (
    ISO_FULL_DATE_PREFIX_PATTERN,
)

if TYPE_CHECKING:
    from hare.fields.data.temporal.datetime_field import DatetimeField
    from hare.fields.data.temporal.time_field import TimeField
if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.data.temporal.datetime_field import DatetimeField
    from hare.fields.data.temporal.time_field import TimeField
    from hare.models import Model

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
        """Parses a raw string for ``to_db_value()`` of a date, datetime or time field, raising
        ``ValidationError`` whatever the parser raises.
        """
        try:
            return parser(value)
        except catch as exc:
            validation_error = field.get_validation_error(exc, value)
        raise validation_error

    @staticmethod
    def raise_if_not_full_date(field: Field[Any], value: str) -> None:
        """Rejects a DateField/DatetimeField string that doesn't start with a full calendar date
        (``2024``, ``2024-05``) - kept here for the reason ``parse_text()`` gives.

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
    def get_auto_now_value(
        field: DatetimeField[Any] | TimeField[Any],
        instance: type[Model] | Model | None,
        now_factory: Callable[[], Any],
    ) -> Any | None:
        """The value an ``auto_now``/``auto_now_add`` write stores, synced onto the instance as a read
        would give it - None when this write isn't one, and ``to_db_value()`` validates ``value`` as
        usual. ``now_factory`` is called only when needed.
        """
        if hasattr(instance, "_saved_in_db") and (
            field.auto_now or (field.auto_now_add and getattr(instance, field.model_field_name) is None)
        ):
            now = now_factory()
            now_python = field.to_python(now)
            setattr(instance, field.model_field_name, now_python)
            return now
        return None

    @staticmethod
    def check_auto_now_options(auto_now: bool, auto_now_add: bool) -> None:
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
