from __future__ import annotations

import datetime
import warnings
from typing import TYPE_CHECKING, Any, Literal, TypeVar, overload

from hare.exceptions import ValidationError
from hare.fields.base.field import Field
from hare.fields.constants import (
    NAIVE_INFINITY_DATETIMES,
    SENSITIVE_VALUE_PLACEHOLDER,
)
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.field_lookup import FieldLookup
from hare.fields.data.temporal.parse_datetime import parse_datetime
from hare.fields.data.temporal.temporal_values import TemporalValues

TDatetime = TypeVar("TDatetime", datetime.datetime, datetime.datetime | None)
# A `__year`/`__month`/`__day` filter value may be an int, float or str.
DatetimeFieldQueryValueType = TypeVar("DatetimeFieldQueryValueType", datetime.datetime, int, float, str)


class DatetimeField(Field[TDatetime], datetime.datetime):
    """A datetime field. ``auto_now`` sets the current time on every save, ``auto_now_add`` on the
    first save only - at most one of them.
    """

    SQL_TYPE = "TIMESTAMP"

    @overload
    def __init__(
        self: DatetimeField[datetime.datetime],
        auto_now: bool = False,
        auto_now_add: bool = False,
        *,
        null: Literal[False] = False,
        **kwargs: Any,
    ) -> None: ...

    @overload
    def __init__(
        self: DatetimeField[datetime.datetime | None],
        auto_now: bool = False,
        auto_now_add: bool = False,
        *,
        null: Literal[True],
        **kwargs: Any,
    ) -> None: ...

    def __init__(self, auto_now: bool = False, auto_now_add: bool = False, **kwargs: Any) -> None:
        TemporalValues.check_auto_now_options(auto_now, auto_now_add)
        super().__init__(**kwargs)
        self.auto_now = auto_now
        self.auto_now_add = auto_now_add

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The generic lookups and the date parts of the value (``__year``, ``__hour``, ...)."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.constants import DATETIME_DATE_PART_LOOKUPS
        from hare.query.filters.field_lookups import FieldLookups

        return FieldLookups.get_date_parts(self, DATETIME_DATE_PART_LOOKUPS)

    @staticmethod
    def get_auto_now_value(now: datetime.datetime | None = None) -> datetime.datetime:
        """The value an auto_now/auto_now_add write stores.

        Args:
            now: The moment to stamp, defaulting to ``Timezone.now()``.

        Returns:
            The current moment, aware UTC under ``use_tz=True``, naive local time otherwise.
        """
        return now if now is not None else Timezone.now()

    def to_python(self, value: Any) -> datetime.datetime | None:
        use_tz = Timezone.get_use_tz()
        return self.get_python_value_with_tz(value, use_tz, Timezone.default() if use_tz else None)

    def get_python_value_with_tz(
        self, value: Any, use_tz: bool, tz: datetime.tzinfo | None
    ) -> datetime.datetime | None:
        """Converts a value to the datetime the field holds - a naive one read in the configured
        zone - with the time zone configuration resolved once by the caller.

        Args:
            value: The value.
            use_tz: Whether time zone support is active.
            tz: The configured zone, when it is.

        Returns:
            The datetime, or None.

        Raises:
            ValidationError: The value isn't a datetime or falls outside the datetime range.
        """
        if value is not None:
            if not isinstance(value, datetime.datetime):
                value = self.get_datetime_value(value)
            try:
                return self._convert_to_configured_zone(value, use_tz, tz)
            except OverflowError:
                raise ValidationError(
                    f"{self.model_field_name}: {self.get_value_for_message(value)} is out of the supported "
                    "datetime range once converted to the configured time zone"
                ) from None
        return value

    @staticmethod
    def _convert_to_configured_zone(
        value: datetime.datetime, use_tz: bool, tz: datetime.tzinfo | None
    ) -> datetime.datetime:
        """Converts a datetime to the configured zone (aware) or the system's local time (naive).

        Args:
            value: The datetime.
            use_tz: Whether time zone support is active.
            tz: The configured zone, when it is.

        Returns:
            The converted datetime.

        Raises:
            OverflowError: The converted value falls outside the datetime range.
        """
        if use_tz:
            # When use_tz=True, ensure all datetimes are timezone-aware
            if value.tzinfo is None and value in NAIVE_INFINITY_DATETIMES:
                # asyncpg decodes TIMESTAMPTZ 'infinity'/'-infinity' as a naive
                # datetime.max/datetime.min - both stand for UTC instants, not wall clocks
                # in the configured zone.
                value = value.replace(tzinfo=datetime.UTC).astimezone(tz)
            elif Timezone.is_naive(value):
                # Timezone.make_aware() detects a nonexistent wall clock (a DST gap), which
                # replace(tzinfo=) wouldn't.
                value = Timezone.make_aware(value, tz)
            else:
                value = value.astimezone(tz)
        else:
            # use_tz=False keeps datetimes naive: an aware one is converted to the local zone before
            # its tzinfo is dropped - a naive value is written as local time.
            if Timezone.is_aware(value):
                if (
                    value.utcoffset() == datetime.timedelta(0)
                    and value.replace(tzinfo=None) in NAIVE_INFINITY_DATETIMES
                ):
                    # The Rust driver decodes TIMESTAMPTZ 'infinity'/'-infinity' as an aware
                    # datetime.max/datetime.min - the same infinity asyncpg gives as a naive one.
                    return value.replace(tzinfo=None)
                value = Timezone.get_system_local_naive(value)
        return value

    def get_datetime_value(self, value: Any) -> datetime.datetime:
        """Converts an accepted input into a datetime.

        Args:
            value: A datetime, a date, an ISO 8601 string or an epoch integer.

        Returns:
            The datetime, naive or aware as given (an epoch integer gives an aware UTC one); for a
            date its first moment in the configured zone under ``use_tz=True`` (midnight, or the
            end of a DST gap starting at midnight), its naive midnight otherwise.

        Raises:
            ValidationError: The value has another type or doesn't denote a datetime.
        """
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, datetime.date):
            if Timezone.get_use_tz():
                return Timezone.get_start_of_day(value)
            return datetime.datetime.combine(value, datetime.time.min)
        if isinstance(value, int) and not isinstance(value, bool):
            # An epoch timestamp is always UTC-based - without tz= fromtimestamp() would read it
            # in the system's local zone and return a naive, shifted wall clock.
            try:
                return datetime.datetime.fromtimestamp(value, tz=datetime.UTC)
            except OverflowError, OSError, ValueError:
                raise ValidationError(
                    f"{self.model_field_name}: epoch timestamp {self.get_value_for_message(value)} is out of "
                    "the supported datetime range"
                ) from None
        if isinstance(value, str):
            TemporalValues.raise_if_not_full_date(self, value)
            return TemporalValues.parse_text(self, value, parse_datetime)
        raise ValidationError(
            f"{self.model_field_name}: expected a datetime, a date, an ISO 8601 string or an epoch integer, "
            f"got {type(value).__name__}"
        )

    def get_assign_normalized_types(self) -> frozenset[type]:
        # The zone a value is converted to depends on the timezone configuration at the time.
        return frozenset()

    def to_db_value(
        self, value: DatetimeFieldQueryValueType | None, instance: type[Model] | Model
    ) -> DatetimeFieldQueryValueType | None:
        return self.get_storage_value(value, instance, stores_utc_instants=False)

    def to_db_instant_value(
        self, value: DatetimeFieldQueryValueType | None, instance: type[Model] | Model | None
    ) -> DatetimeFieldQueryValueType | None:
        """Like ``to_db_value()``, for a column that stores UTC instants: under ``use_tz=False`` a
        naive value is bound as the instant of its system-local wall clock.

        Args:
            value: The value.
            instance: The model (class) the value is written or compared for.

        Returns:
            The value to bind.
        """
        return self.get_storage_value(value, instance, stores_utc_instants=True)

    def get_storage_value(
        self,
        value: DatetimeFieldQueryValueType | None,
        instance: type[Model] | Model | None,
        *,
        stores_utc_instants: bool,
    ) -> DatetimeFieldQueryValueType | None:
        """The value bound for a column of this field, auto_now/auto_now_add applied.

        Args:
            value: The value.
            instance: The model (class) the value is written or compared for.
            stores_utc_instants: Whether the column stores UTC instants rather than wall clocks.

        Returns:
            The value to bind.
        """
        auto_value = TemporalValues.get_auto_now_value(self, instance, DatetimeField.get_auto_now_value)
        if auto_value is not None:
            value = auto_value
        if value is not None:
            # Every accepted input becomes a datetime here, whatever the write path - a bare `date`
            # becomes its first moment.
            value = self.get_db_datetime_value(value, stores_utc_instants)  # type: ignore[assignment]
        if auto_value is None:
            self.validate(value)
        return value

    def get_db_datetime_value(self, value: Any, stores_utc_instants: bool) -> datetime.datetime:
        """An accepted input as the datetime bound for a column of this field.

        Args:
            value: A datetime, a date, an ISO 8601 string or an epoch integer.
            stores_utc_instants: Whether the column stores UTC instants rather than wall clocks.

        Returns:
            The datetime to bind.

        Raises:
            ValidationError: The value isn't a datetime, or falls outside the datetime range once
                converted to UTC.
        """
        datetime_value = self.get_datetime_value(value)
        try:
            return self._get_db_datetime(datetime_value, stores_utc_instants)
        except OverflowError:
            raise ValidationError(
                f"{self.model_field_name}: {self.get_value_for_message(value)} is out of the supported "
                "datetime range once converted to UTC"
            ) from None

    def _get_db_datetime(self, value: datetime.datetime, stores_utc_instants: bool) -> datetime.datetime:
        """The datetime to bind: aware under ``use_tz=True``; under ``use_tz=False`` the system's
        naive local time, or its instant for a column that stores UTC instants.

        Args:
            value: The datetime being written or compared.
            stores_utc_instants: Whether the column stores UTC instants rather than wall clocks.

        Returns:
            The datetime to bind.

        Raises:
            OverflowError: The value's UTC instant falls outside the datetime range.
        """
        if Timezone.get_use_tz():
            if Timezone.is_naive(value):
                shown_value = SENSITIVE_VALUE_PLACEHOLDER if self.sensitive else value
                warnings.warn(
                    f"DateTimeField {self.model_field_name} received a naive datetime ({shown_value})"
                    " while time zone support is active.",
                    RuntimeWarning,
                )
                # The configured zone, as from_db_value() reads a naive value - create() and
                # .filter() with the same naive value must mean the same instant.
                value = Timezone.make_aware(value, Timezone.default())
            if value.year in (datetime.MINYEAR, datetime.MAXYEAR):
                # Every backend stores the UTC instant; one past the datetime range can't be
                # written - only reachable in the first and last year.
                value.astimezone(datetime.UTC)
            return value
        # use_tz=False: a wall-clock column stores the system's local time, as from_db_value()
        # reads it back; a UTC-instant column gets the instant itself.
        if Timezone.is_aware(value):
            local_value = Timezone.get_system_local_naive(value)
            return value if stores_utc_instants else local_value
        if value in NAIVE_INFINITY_DATETIMES:
            return value
        if value.year in (datetime.MINYEAR, datetime.MAXYEAR):
            Timezone.make_system_local_aware(value).astimezone(datetime.UTC)
        if stores_utc_instants:
            return Timezone.make_system_local_aware(value)
        return value

    @property
    def constraints(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.auto_now_add or self.auto_now:
            data["readOnly"] = True
        return data
