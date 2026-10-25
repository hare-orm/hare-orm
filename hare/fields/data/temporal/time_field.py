from __future__ import annotations

import datetime
import warnings
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, overload

from hare.core.constants import HARE_PACKAGE_DIRECTORY
from hare.exceptions import ValidationError
from hare.fields.constants import (
    SENSITIVE_VALUE_PLACEHOLDER,
)
from hare.fields.db_defaults.now import Now
from hare.fields.enums import NowValueType
from hare.fields.field import Field
from hare.sql.enums import DateTruncSource
from hare.time import Timezone

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.fields.data.temporal.temporal_values import TemporalValues

TTime = TypeVar("TTime", datetime.time, datetime.time | None)


class TimeField(Field[TTime]):
    """A time of day - a column of the database's time type with a UTC offset where the database has one,
    else its text. Under ``use_timezone=True`` a naive time takes the configured zone's fixed offset, and
    without it an offset is dropped."""

    field_type = datetime.time
    date_function_source = DateTruncSource.TIME

    keeps_native_db_values = True
    SQL_TYPE = "TIME"

    @overload
    def __init__(
        self: TimeField[datetime.time],
        auto_now: bool = False,
        auto_now_add: bool = False,
        *,
        null: Literal[False] = False,
        **kwargs: Any,
    ) -> None: ...

    @overload
    def __init__(
        self: TimeField[datetime.time | None],
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
        if isinstance(self.db_default, Now):
            self.db_default = self.db_default.get_for_value_type(NowValueType.TIME)

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The generic lookups and the date parts of the value (``__hour``, ``__minute``, ...)."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.constants import TIME_OF_DAY_DATE_PART_LOOKUPS
        from hare.query.filters.lookups.field_lookups import FieldLookups

        return FieldLookups.get_date_parts(self, TIME_OF_DAY_DATE_PART_LOOKUPS)

    @staticmethod
    def get_auto_now_value(now: datetime.datetime | None = None) -> datetime.time:
        """The value an ``auto_now``/``auto_now_add`` write stores: the current local wall-clock time,
        under ``use_timezone=True`` with the configured zone's fixed standard offset, as every naive time
        written gets.

        Args:
            now: The moment to stamp, ``Timezone.now()`` by default.

        Returns:
            An aware time under ``use_timezone=True``, a naive one otherwise.
        """
        if now is None:
            now = Timezone.now()
        if Timezone.get_use_timezone():
            return Timezone.localtime(now).time().replace(tzinfo=Timezone.get_fixed_offset())
        return now.time()

    def get_stamp_value(self, now: datetime.datetime | None = None) -> datetime.time:
        """The value an ``auto_now``/``auto_now_add`` write gives an instance: the time of the moment as a
        read gives it back.

        Args:
            now: The moment, the current one by default.

        Returns:
            The value.
        """
        return cast("datetime.time", self.to_python(self.get_auto_now_value(now)))

    def to_python(self, value: Any) -> datetime.time | None:
        self.raise_if_duration(value)
        # A timedelta is refused above - what is read is a time or None.
        return cast("datetime.time | None", self.from_db_value(value))

    def from_db_value(self, value: Any) -> datetime.time | datetime.timedelta | None:
        if value is not None:
            if isinstance(value, str):
                value = TemporalValues.parse_time_of_day_text(self, value)
            if isinstance(value, datetime.timedelta):
                # A duration a driver reads from a column - kept as it is read.
                return value
            self.raise_if_not_time(value)
            if Timezone.get_use_timezone():
                if Timezone.is_naive(value):
                    # A fixed offset, not the configured zone itself: a bare time has no date for
                    # a named zone to resolve its offset against, so attaching one leaves the
                    # value still naive as far as Python (and every driver) is concerned.
                    value = value.replace(tzinfo=Timezone.get_fixed_offset())
            else:
                if Timezone.is_aware(value):
                    value = value.replace(tzinfo=None)
        return value

    def get_assign_normalized_types(self) -> frozenset[type]:
        # Whether tzinfo is attached or stripped depends on the timezone configuration at the time.
        return frozenset()

    def raise_if_not_time(self, value: Any) -> None:
        """Rejects a value that is no time.

        Args:
            value: The value, a string already parsed.

        Raises:
            ValidationError: The value has another type.
        """
        if not isinstance(value, datetime.time):
            raise ValidationError(
                f"{self.model_field_name}: expected a time or an ISO 8601 string, got {type(value).__name__}"
            )

    def raise_if_duration(self, value: Any) -> None:
        """Rejects a timedelta given for the field - a span of time is no time of day.

        Args:
            value: The value given.

        Raises:
            ValidationError: ``value`` is a timedelta.
        """
        if isinstance(value, datetime.timedelta):
            raise ValidationError(f"{self.model_field_name}: a timedelta is no time of day - write a datetime.time")

    def to_db_value(
        self,
        value: datetime.time | datetime.timedelta | None,
        instance: type[Model] | Model | None,
    ) -> datetime.time | None:
        if value is not None:
            if isinstance(value, str):
                # An assigned string is parsed here, as from_db_value() does.
                value = TemporalValues.parse_time_of_day_text(self, value)
            # A span of time is no time of day - one database refused it, another stored its text.
            self.raise_if_duration(value)
            self.raise_if_not_time(value)
            value = cast("datetime.time", value)
            if Timezone.get_use_timezone():
                if Timezone.is_naive(value):
                    shown_value = SENSITIVE_VALUE_PLACEHOLDER if self.sensitive else value
                    warnings.warn(
                        f"TimeField {self.model_field_name} received a naive time ({shown_value})"
                        " while time zone support is active.",
                        RuntimeWarning,
                        skip_file_prefixes=(HARE_PACKAGE_DIRECTORY,),
                    )
                    # Same reasoning as from_db_value's own naive branch: a bare time needs a
                    # fixed offset, since a named zone has no date to resolve one against.
                    value = value.replace(tzinfo=Timezone.get_fixed_offset())
            elif Timezone.is_aware(value):
                # use_timezone=False: an assigned aware time is made naive, as a read one is.
                value = value.replace(tzinfo=None)
        self.validate(value)
        return value

    @property
    def constraints(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.auto_now_add or self.auto_now:
            data["readOnly"] = True
        return data
