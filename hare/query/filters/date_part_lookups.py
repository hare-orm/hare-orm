from __future__ import annotations

import datetime
import operator
from collections.abc import Callable
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.exceptions import (
    UnSupportedError,
)
from hare.query.enums import Lookup, LookupValueShape
from hare.query.filters.constants import (
    DATE_PART_COMPARISON_LOOKUPS,
)
from hare.query.filters.field_lookup import FieldLookup
from hare.sql.enums import DatePart, DatetimeCastTarget
from hare.sql.functions.datetime_cast import DatetimeCast
from hare.sql.functions.extract import Extract
from hare.sql.terms.base.term import Term
from hare.sql.terms.criteria.criterion import Criterion

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
from hare.query.filters.lookups import Lookups
from hare.utils import Timezone


class DatePartLookups:
    """``field__year``, ``field__year__gte``, ``field__week_day__in``, ... and a datetime's
    ``field__date``/``field__time`` - the extracted part (or the date/time of day, in the
    configured zone) compared like a column of its own."""

    @staticmethod
    def get_zone(is_datetime_field: bool) -> tuple[str | None, bool]:
        """The zone a datetime column is read in.

        Returns:
            ``(zone name under use_tz, whether a naive value is read in the local zone)``.
        """
        use_tz = Timezone.get_use_tz()
        zone_name = Timezone.name() if is_datetime_field and use_tz else None
        return zone_name, is_datetime_field and not use_tz

    @classmethod
    def compare_part(
        cls,
        date_part: DatePart,
        comparison: Callable[..., Criterion],
        field: Term,
        value: Any,
        *,
        is_datetime_field: bool = False,
    ) -> Criterion:
        """Compares a date part of ``field`` with an already encoded ``value``."""
        zone_name, use_local_zone_when_naive = cls.get_zone(is_datetime_field)
        term = Extract(date_part, field, zone_name=zone_name, use_local_zone_when_naive=use_local_zone_when_naive)
        return comparison(term, value)

    @classmethod
    def compare_cast(
        cls, target: DatetimeCastTarget, comparison: Callable[..., Criterion], field: Term, value: Any
    ) -> Criterion:
        """Compares a datetime column's date or time of day with an already encoded ``value``."""
        zone_name, use_local_zone_when_naive = cls.get_zone(True)
        term = DatetimeCast(target, field, zone_name=zone_name, use_local_zone_when_naive=use_local_zone_when_naive)
        return comparison(term, value)

    @staticmethod
    def encode_values(
        encode: Callable[[Any], Any], value_type: LookupValueShape, lookup_name: str, values: Any
    ) -> Any:
        """Encodes one value, a list or a range with ``encode``; ``None`` stays ``None``.

        Raises:
            UnSupportedError: A list/range lookup got something else than a list/tuple/set.
        """
        if value_type == LookupValueShape.VALUE:
            return encode(values)
        if not isinstance(values, (list, tuple, set)):
            raise UnSupportedError(f"__{lookup_name} expects a list/tuple/set of values, got {values!r}")
        return [value if value is None else encode(value) for value in values]

    @classmethod
    def encode_part(
        cls,
        date_part: DatePart,
        value_type: LookupValueShape,
        lookup_name: str,
        value: Any,
        instance: Any,
        field: Any,
        dialect: Dialect,
    ) -> Any:
        """Encodes a date-part lookup value as the int(s) it compares with."""
        return cls.encode_values(partial(DatePartLookups.get_part_value, date_part), value_type, lookup_name, value)

    @staticmethod
    def get_date(lookup_name: str, value: Any) -> datetime.date:
        """A ``__date`` lookup value as a date.

        Raises:
            UnSupportedError: The value isn't a date, a datetime or an ISO date string.
        """
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str):
            try:
                return datetime.date.fromisoformat(value)
            except ValueError:
                pass
        raise UnSupportedError(f"__{lookup_name} expects a date, got {value!r}")

    @staticmethod
    def get_time(lookup_name: str, value: Any) -> datetime.time:
        """A ``__time`` lookup value as a naive time.

        Raises:
            UnSupportedError: The value isn't a time or an ISO time string.
        """
        if isinstance(value, str):
            try:
                value = datetime.time.fromisoformat(value)
            except ValueError:
                pass
        if isinstance(value, datetime.time):
            return value.replace(tzinfo=None)
        raise UnSupportedError(f"__{lookup_name} expects a time, got {value!r}")

    @classmethod
    def encode_cast(
        cls,
        target: DatetimeCastTarget,
        value_type: LookupValueShape,
        lookup_name: str,
        value: Any,
        instance: Any,
        field: Any,
        dialect: Dialect,
    ) -> Any:
        """Encodes a ``__date``/``__time`` lookup value as what the dialect compares a datetime's
        date or time of day with (``Dialect.get_datetime_part_comparand``)."""

        def encode(single_value: Any) -> Any:
            if target == DatetimeCastTarget.DATE:
                return dialect.get_datetime_part_comparand(cls.get_date(lookup_name, single_value))
            return dialect.get_datetime_part_comparand(cls.get_time(lookup_name, single_value))

        return cls.encode_values(encode, value_type, lookup_name, value)

    @staticmethod
    def get_comparison(comparison_name: str) -> Callable[..., Criterion]:
        """The criterion builder of a chained lookup name."""
        comparisons: dict[str, Callable[..., Criterion]] = {
            Lookup.EXACT: operator.eq,
            Lookup.NOT: Lookups.not_equal,
            Lookup.GT: operator.gt,
            Lookup.GTE: operator.ge,
            Lookup.LT: operator.lt,
            Lookup.LTE: operator.le,
            Lookup.IN: Lookups.is_in,
            Lookup.NOT_IN: Lookups.not_in,
            Lookup.RANGE: Lookups.between,
        }
        return comparisons[comparison_name]

    @classmethod
    def get_lookups(cls, date_part_lookups: dict[str, DatePart], *, is_datetime_field: bool) -> dict[str, FieldLookup]:
        """Builds every date-part lookup of one field, with ``__date``/``__time`` for a datetime.

        Args:
            date_part_lookups: Lookup suffix to the date part it extracts.
            is_datetime_field: The field holds datetimes.

        Returns:
            The lookups by suffix - ``part`` plus an optional comparison suffix.
        """
        lookups: dict[str, FieldLookup] = {}
        for comparison_name, value_type in DATE_PART_COMPARISON_LOOKUPS.items():
            comparison = cls.get_comparison(comparison_name)
            suffix = f"__{comparison_name}" if comparison_name else ""
            for lookup_suffix, date_part in date_part_lookups.items():
                lookup_name = f"{lookup_suffix}{suffix}"
                lookups[lookup_name] = FieldLookup(
                    partial(cls.compare_part, date_part, comparison, is_datetime_field=is_datetime_field),
                    partial(cls.encode_part, date_part, value_type, lookup_name),
                )
            if not is_datetime_field:
                continue
            for target in DatetimeCastTarget:
                lookup_name = f"{target.name.lower()}{suffix}"
                lookups[lookup_name] = FieldLookup(
                    partial(cls.compare_cast, target, comparison),
                    partial(cls.encode_cast, target, value_type, lookup_name),
                )
        return lookups

    @staticmethod
    def get_part_value(date_part: DatePart, value: Any) -> int:
        """The int a `__year`/`__month`/... lookup compares with.

        Raises:
            UnSupportedError: `value` isn't an int, a whole-number float/Decimal, or a string
                convertible to an int.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
            raise UnSupportedError(f"__{date_part.name.lower()} expects an int, got {value!r}")
        try:
            integral_value = int(value)
        except ValueError, OverflowError:
            raise UnSupportedError(f"__{date_part.name.lower()} expects an int, got {value!r}") from None
        if isinstance(value, (float, Decimal)) and integral_value != value:
            raise UnSupportedError(f"__{date_part.name.lower()} expects an int, got {value!r}")
        return integral_value

    @staticmethod
    def _extract_equal(date_part: DatePart, field: Term, value: Any, *, is_datetime_field: bool = False) -> Criterion:
        """A date part of ``field`` equal to ``value``. A datetime field's part is read in the
        configured zone, taken from ``Timezone`` on every call. ``value`` is converted to ``int``
        here, so every backend treats a numeric string the same way.

        Raises:
            UnSupportedError: ``value`` isn't an int, a whole float/Decimal or a string of an int.
        """
        value = DatePartLookups.get_part_value(date_part, value)
        use_tz = Timezone.get_use_tz()
        zone_name = Timezone.name() if is_datetime_field and use_tz else None
        # A naive datetime field is read in the machine's local zone where the column holds an
        # instant (PostgreSQL); a dialect storing the wall-clock digits ignores it.
        use_local_zone_when_naive = is_datetime_field and not use_tz
        return Extract(date_part, field, zone_name=zone_name, use_local_zone_when_naive=use_local_zone_when_naive).eq(
            value
        )

    # The date part lookups of a value with no zone of its own (a JSON scalar) - no zone conversion.
    # A datetime field has its own.
    year_equal = partial(_extract_equal, DatePart.YEAR)

    quarter_equal = partial(_extract_equal, DatePart.QUARTER)

    month_equal = partial(_extract_equal, DatePart.MONTH)

    week_equal = partial(_extract_equal, DatePart.WEEK)

    day_equal = partial(_extract_equal, DatePart.DAY)

    hour_equal = partial(_extract_equal, DatePart.HOUR)

    minute_equal = partial(_extract_equal, DatePart.MINUTE)

    second_equal = partial(_extract_equal, DatePart.SECOND)

    microsecond_equal = partial(_extract_equal, DatePart.MICROSECOND)
