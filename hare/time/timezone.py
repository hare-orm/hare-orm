from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, time, timezone as dt_timezone, tzinfo
from zoneinfo import ZoneInfo

from hare.exceptions import NonExistentTimeError, QueryError
from hare.time.constants import DEFAULT_TIMEZONE
from hare.time.system_clock import SystemClock
from hare.time.zone_names import ZoneNames
from hare.time.zone_settings import ZoneSettings


class Timezone:
    """Datetimes against the configured zone - the current moment, a datetime made aware or naive, a
    zone's fixed offset and a day's first moment - and the zone names and settings the documentation
    gives through it."""

    @staticmethod
    def parse(zone: str) -> ZoneInfo:
        """The time zone of a name - ``ZoneNames.parse()``.

        Args:
            zone: The zone's name, in any case.

        Returns:
            The zone.

        Raises:
            ZoneInfoNotFoundError: No zone has the name.
        """
        return ZoneNames.parse(zone)

    @staticmethod
    def name() -> str:
        """The current zone's name - ``ZoneSettings.name()``.

        Returns:
            The IANA zone name.
        """
        return ZoneSettings.name()

    @staticmethod
    def override(zone: str | tzinfo) -> AbstractContextManager[None]:
        """Makes ``zone`` the current zone for a block of code - ``ZoneSettings.override()``.

        Args:
            zone: An IANA zone name or a ``ZoneInfo``.

        Returns:
            The context manager of the block.

        Raises:
            ConfigurationError: The zone isn't a known IANA zone.
        """
        return ZoneSettings.override(zone)

    @staticmethod
    def now() -> datetime:
        """
        The current time - aware or naive by ``use_timezone``.

        When use_timezone=True, returns an aware datetime in UTC.
        When use_timezone=False, returns a naive datetime.
        """
        if ZoneSettings.get_use_timezone():
            return SystemClock.get_utc_now()
        return SystemClock.get_local_now()

    @staticmethod
    def default() -> tzinfo:
        """
        The default time zone, as a tzinfo.

        This is the time zone defined by Hare config.
        """
        return ZoneNames.parse(ZoneSettings.name())

    @staticmethod
    def get_fixed_offset(timezone: tzinfo | str | None = None, at: datetime | None = None) -> tzinfo:
        """A fixed-offset zone for a bare time - a ``time`` has no date, so a zone with DST rules
        reports no offset for it, and ``TIMETZ`` needs one. By default the zone's standard offset,
        so one wall clock saved in winter and in summer stays equal.

        Args:
            timezone: The zone, the configured one by default.
            at: The moment whose offset is taken.

        Returns:
            A fixed-offset tzinfo, or the zone itself when it reports no offset even for a datetime.
        """
        zone = Timezone._get_timezone(timezone)
        if at is not None:
            moment = at
            if Timezone.is_naive(moment):
                moment = moment.replace(tzinfo=UTC)
            offset = moment.astimezone(zone).utcoffset()
        else:
            reference = SystemClock.get_utc_now().astimezone(zone)
            utcoffset = reference.utcoffset()
            daylight_saving_offset = reference.dst()
            offset = (
                utcoffset - daylight_saving_offset
                if utcoffset is not None and daylight_saving_offset is not None
                else utcoffset
            )
        return dt_timezone(offset) if offset is not None else zone

    @staticmethod
    def _get_timezone(timezone: tzinfo | str | None = None) -> tzinfo:
        """
        If timezone is None return the default() timezone;
        else if timezone is tzinfo object, return it;
        else parse string to ZoneInfo instance.
        """
        if timezone is None:
            return Timezone.default()
        return ZoneNames.parse(timezone) if isinstance(timezone, str) else timezone

    @staticmethod
    def localtime(value: datetime | None = None, timezone: tzinfo | str | None = None) -> datetime:
        """Converts an aware datetime - now by default - to the current zone, or to ``timezone``.

        Raises:
            QueryError: ``value`` is naive.
        """
        if value is None:
            value = Timezone.now()
        elif Timezone.is_naive(value):
            raise QueryError("localtime() cannot be applied to a naive datetime")
        zone = Timezone._get_timezone(timezone)
        return value.astimezone(zone)

    @staticmethod
    def is_aware(value: datetime | time) -> bool:
        """Whether a datetime or time is aware - ``utcoffset()`` isn't None."""
        return value.utcoffset() is not None

    @staticmethod
    def is_naive(value: datetime | time) -> bool:
        """Whether a datetime or time is naive - ``utcoffset()`` is None."""
        return value.utcoffset() is None

    @staticmethod
    def _is_nonexistent_local_time(value: datetime, timezone: tzinfo) -> bool:
        """Whether a naive wall clock doesn't exist in ``timezone`` - skipped by a DST spring-forward.
        Converting it with its fold=0 offset to UTC and back lands on another wall clock; an
        ambiguous one comes back unchanged.

        Args:
            value: A naive datetime.
            timezone: The zone.

        Returns:
            True for a nonexistent local time.
        """
        fold0 = value.replace(tzinfo=timezone, fold=0)
        fold1 = value.replace(tzinfo=timezone, fold=1)
        if fold0.utcoffset() == fold1.utcoffset():
            return False
        round_trip = fold0.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
        return round_trip != value

    @staticmethod
    def get_start_of_day(day: date, timezone: tzinfo | str | None = None) -> datetime:
        """The first moment of a day in a zone - midnight, or the end of a DST gap starting at it.

        Args:
            day: The calendar day.
            timezone: The zone, defaulting to the configured one.

        Returns:
            The aware first moment of ``day``.
        """
        zone = Timezone._get_timezone(timezone)
        midnight = datetime.combine(day, time.min)
        if hasattr(zone, "localize"):
            return zone.normalize(zone.localize(midnight, is_dst=True))  # type: ignore[attr-defined]
        # fold=0 reads a wall clock inside a gap with the offset before it - converting that
        # instant back gives the wall clock right after the gap, the day's first real moment.
        aware_midnight = midnight.replace(tzinfo=zone, fold=0)
        try:
            return aware_midnight.astimezone(UTC).astimezone(zone)
        except OverflowError:
            return aware_midnight

    @staticmethod
    def make_aware(value: datetime, timezone: tzinfo | str | None = None, is_dst: bool | None = None) -> datetime:
        """Makes a naive datetime aware in ``timezone``. ``is_dst`` picks the reading of an ambiguous
        wall clock (a DST fall-back).

        Raises:
            QueryError: ``value`` isn't naive.
            NonExistentTimeError: The wall clock is skipped by a DST spring-forward.
        """
        zone = Timezone._get_timezone(timezone)
        if hasattr(zone, "localize"):
            # pytz's own localize() already raises pytz.exceptions.NonExistentTimeError (a
            # ValueError subclass) for a spring-forward gap when is_dst is None - no separate
            # check needed on this branch.
            return zone.localize(value, is_dst=is_dst)
        if Timezone.is_aware(value):
            raise QueryError(f"make_aware expects a naive datetime, got {value}")
        # UTC skips no wall clock.
        if getattr(zone, "key", None) != DEFAULT_TIMEZONE and Timezone._is_nonexistent_local_time(value, zone):
            raise NonExistentTimeError(
                f"{value} is a nonexistent local time in {getattr(zone, 'key', zone)} due to a DST transition"
            )
        if is_dst is None:
            return value.replace(tzinfo=zone)
        # ZoneInfo has no localize() - is_dst is a fold: 0 the earlier (daylight) reading, 1 the
        # later one.
        return value.replace(tzinfo=zone, fold=0 if is_dst else 1)

    @staticmethod
    def make_naive(value: datetime, timezone: tzinfo | str | None = None) -> datetime:
        """An aware datetime as the naive one of a time zone.

        Raises:
            QueryError: The given datetime is naive.
        """
        zone = Timezone._get_timezone(timezone)
        if Timezone.is_naive(value):
            raise QueryError("make_naive() cannot be applied to a naive datetime")
        return value.astimezone(zone).replace(tzinfo=None)
