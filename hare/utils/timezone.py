from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, time, timezone as dt_timezone, tzinfo
from types import ModuleType
from typing import TYPE_CHECKING, ClassVar, cast
from zoneinfo import ZoneInfoNotFoundError

from hare.core.cache import Cache
from hare.exceptions import ConfigurationError, NonExistentTimeError, QueryError
from hare.utils.constants import DEFAULT_TIMEZONE, SYSTEM_ZONE_FALLBACK_REFERENCE_MOMENT, ZONE_CACHE_SIZE

if TYPE_CHECKING:
    from hare.core.context import HareContext
from hare.utils.zone_info import ZoneInfo


class Timezone:
    """Timezone handling: reading Hare's configured use_tz/timezone settings, and
    converting/checking datetimes against them."""

    #: (zone name,) -> its zone.
    ZONES: ClassVar[Cache[ZoneInfo]] = Cache(ZONE_CACHE_SIZE)
    #: The zone of the system's local time when it can't be read - found on first use.
    system_zone_fallback: ClassVar[tzinfo | None] = None

    #: The zone set for a block of code with ``override()``; None for Hare's configured zone.
    current_override: ClassVar[ContextVar[str | None]] = ContextVar("hare_timezone_override", default=None)

    #: ``hare.core.context.HareContext``, bound on first use by ``get_current_context()`` - that
    #: package imports this module, so importing it here at module level would be circular.
    context_class: ClassVar[type[HareContext] | None] = None

    #: The optional ``tzlocal`` module, imported by the first ``get_tzlocal()`` rather than with
    #: hare; None without it installed.
    tzlocal: ClassVar[ModuleType | None] = None
    #: Whether ``tzlocal`` was looked for.
    tzlocal_loaded: ClassVar[bool] = False

    @staticmethod
    def get_tzlocal() -> ModuleType | None:
        """The ``tzlocal`` module, imported now when it wasn't.

        Returns:
            The module, None without it installed.
        """
        if not Timezone.tzlocal_loaded:
            try:
                import tzlocal
            except ImportError:  # pragma: nocoverage
                Timezone.tzlocal = None
            else:
                Timezone.tzlocal = tzlocal
            Timezone.tzlocal_loaded = True
        return Timezone.tzlocal

    @staticmethod
    def get_local_zone_name() -> str:
        """The IANA zone name of the machine this process runs on - independent of Hare's own
        configured ``use_tz``/``timezone``.

        Raises:
            ConfigurationError: the optional ``tzlocal`` dependency isn't installed.
        """
        tzlocal = Timezone.get_tzlocal()
        if tzlocal is None:
            raise ConfigurationError(
                "Determining the local system time zone requires the optional 'tzlocal' "
                "dependency - install it (pip install hare-orm[tzlocal]) or set use_tz=True to "
                "avoid needing to know the local zone at all."
            )
        return cast("str", tzlocal.get_localzone_name())

    @staticmethod
    def get_system_zone_fallback() -> tzinfo:
        """The system's local zone for an instant ``datetime.astimezone()`` can't convert (any
        instant before 1970 on Windows).

        Returns:
            The IANA zone ``tzlocal`` names, or - without it - a fixed offset: the system's own
            offset at ``SYSTEM_ZONE_FALLBACK_REFERENCE_MOMENT``.
        """
        if Timezone.system_zone_fallback is None:
            Timezone.system_zone_fallback = Timezone.find_system_zone_fallback()
        return Timezone.system_zone_fallback

    @staticmethod
    def find_system_zone_fallback() -> tzinfo:
        """The zone ``get_system_zone_fallback()`` keeps."""
        tzlocal = Timezone.get_tzlocal()
        if tzlocal is not None:
            try:
                return Timezone.parse(tzlocal.get_localzone_name())
            except LookupError, ValueError, OSError:
                pass
        offset = SYSTEM_ZONE_FALLBACK_REFERENCE_MOMENT.astimezone().utcoffset()
        return dt_timezone(offset) if offset is not None else UTC

    @staticmethod
    def get_system_local_naive(value: datetime) -> datetime:
        """An aware datetime as the system's local wall-clock time.

        Args:
            value: An aware datetime.

        Returns:
            The naive local wall-clock time of the same instant.

        Raises:
            OverflowError: The local time falls outside the datetime range.
        """
        try:
            local_value = value.astimezone()
        except OSError, OverflowError, ValueError:
            local_value = value.astimezone(Timezone.get_system_zone_fallback())
        return local_value.replace(tzinfo=None)

    @staticmethod
    def make_system_local_aware(value: datetime) -> datetime:
        """A naive datetime read as the system's local wall-clock time.

        Args:
            value: A naive datetime.

        Returns:
            The aware datetime of that local wall-clock time.
        """
        try:
            return value.astimezone()
        except OSError, OverflowError, ValueError:
            return value.replace(tzinfo=Timezone.get_system_zone_fallback())

    @staticmethod
    def parse(zone: str) -> tzinfo:
        cache_key = DEFAULT_TIMEZONE if zone.upper() == DEFAULT_TIMEZONE else zone
        cached = Timezone.ZONES.get((cache_key,))
        if cached is not None:
            return cached
        try:
            zone_info = ZoneInfo(cache_key)
        except ZoneInfoNotFoundError as e:
            words = zone.split("/")
            # As pytz.timezone spells it: US/central -> US/Central, asia/ShangHai -> Asia/Shanghai.
            styled = "/".join([i if i.isupper() else i.title() for i in words])
            if styled != zone:
                return Timezone.parse(styled)
            raise e
        Timezone.ZONES[(cache_key,)] = zone_info
        return zone_info

    @staticmethod
    def validate(zone: object) -> None:
        """Checks that a configured timezone name is a known IANA zone.

        Args:
            zone: The configured timezone.

        Raises:
            ConfigurationError: If the zone is not a string or is unknown.
        """
        if not isinstance(zone, str):
            raise ConfigurationError(f"timezone must be a string, got {type(zone).__name__}")
        if zone.upper() == DEFAULT_TIMEZONE:
            # Always known - the zone database ships with the system or, on Windows, with hare's
            # tzdata dependency. Its zone data is read on first use: loading it through tzdata
            # costs a few milliseconds of every Hare.init().
            return
        try:
            Timezone.parse(zone)
        except ZoneInfoNotFoundError, ValueError:
            raise ConfigurationError(f"Unknown timezone {zone!r}: expected an IANA zone name like 'UTC'") from None

    @staticmethod
    def get_use_tz() -> bool:
        """
        Get use_tz from the active HareContext, or True if no context is active.
        """
        ctx = Timezone.get_current_context()
        return ctx._use_tz if ctx is not None else True

    @staticmethod
    def name() -> str:
        """The current zone's name: the one ``override()`` sets for a block of code, else the
        active HareContext's configured one, else the default.

        Returns:
            The IANA zone name.
        """
        override = Timezone.current_override.get()
        if override is not None:
            return override
        ctx = Timezone.get_current_context()
        return ctx._timezone if ctx is not None else DEFAULT_TIMEZONE

    @staticmethod
    def get_aware_zone_name() -> str | None:
        """The zone aware datetimes are read and written in: ``name()`` under ``use_tz``, else None -
        one lookup of the active HareContext for both.

        Returns:
            The IANA zone name, or None when datetimes are naive.
        """
        ctx = Timezone.get_current_context()
        if ctx is not None and not ctx._use_tz:
            return None
        override = Timezone.current_override.get()
        if override is not None:
            return override
        return ctx._timezone if ctx is not None else DEFAULT_TIMEZONE

    @staticmethod
    def get_rendered_zone_name() -> str | None:
        """The zone a query renders into its SQL text: ``name()`` under ``use_tz`` (a
        ``field__year`` lookup extracts in it), the machine's own zone without it (a datetime
        expression is converted from it) - None when that one can't be known (no ``tzlocal``), and
        a query rendering it fails anyway.

        Returns:
            The IANA zone name, or None.
        """
        ctx = Timezone.get_current_context()
        if ctx is not None and not ctx._use_tz:
            tzlocal = Timezone.get_tzlocal()
            return tzlocal.get_localzone_name() if tzlocal is not None else None
        override = Timezone.current_override.get()
        if override is not None:
            return override
        return ctx._timezone if ctx is not None else DEFAULT_TIMEZONE

    @staticmethod
    def get_current_context() -> HareContext | None:
        """The active HareContext - read on every datetime conversion; its ``use_tz`` and
        ``timezone`` are read here without their properties' calls.

        Returns:
            The context, or None when none is active.
        """
        context_class = Timezone.context_class
        if context_class is None:
            from hare.core.context import HareContext

            context_class = Timezone.context_class = HareContext
        # HareContext.get_current(), without its call - read on every query and datetime conversion.
        context = context_class.current_context.get()
        return context if context is not None else context_class.global_context

    @staticmethod
    @contextmanager
    def override(zone: str | tzinfo) -> Generator[None]:
        """Makes ``zone`` the current zone for a block of code - and the tasks it starts - in place
        of the configured one, like Django's ``timezone.override()``: the date and time parts of a
        datetime (``__date``, ``__year``, ``__hour`` lookups, ``Extract*``, ``Trunc*``) are taken in
        it, and a naive datetime is read in it. Datetimes still come back as the same moments::

            with Timezone.override("Europe/Moscow"):
                today = await Order.filter(created_at__date=moscow_today).count()

        Args:
            zone: An IANA zone name or a ``ZoneInfo``.

        Raises:
            ConfigurationError: The zone isn't a known IANA zone.
        """
        token = Timezone.current_override.set(Timezone.get_zone_name(zone))
        try:
            yield
        finally:
            Timezone.current_override.reset(token)

    @staticmethod
    def get_zone_name(zone: str | tzinfo) -> str:
        """The IANA name of a zone given by name or as a ``ZoneInfo``.

        Args:
            zone: The zone.

        Returns:
            Its IANA name.

        Raises:
            ConfigurationError: The zone is unknown, or a tzinfo without an IANA name (a fixed
                offset) - the database converts by name.
        """
        if isinstance(zone, str):
            Timezone.validate(zone)
            return zone
        zone_key = getattr(zone, "key", None)
        if isinstance(zone_key, str):
            return zone_key
        raise ConfigurationError(f"A time zone must be an IANA zone name or a ZoneInfo, got {zone!r}")

    @staticmethod
    def now() -> datetime:
        """
        Return a datetime.datetime, aware or naive depending on use_tz setting.

        When use_tz=True, returns an aware datetime in UTC.
        When use_tz=False, returns a naive datetime.
        """
        if Timezone.get_use_tz():
            return datetime.now(tz=UTC)
        else:
            return datetime.now()

    @staticmethod
    def default() -> tzinfo:
        """
        Return the default time zone as a tzinfo instance.

        This is the time zone defined by Hare config.
        """
        return Timezone.parse(Timezone.name())

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
        tz = Timezone._get_timezone(timezone)
        if at is not None:
            moment = at
            if Timezone.is_naive(moment):
                moment = moment.replace(tzinfo=UTC)
            offset = moment.astimezone(tz).utcoffset()
        else:
            reference = datetime.now(tz=UTC).astimezone(tz)
            utcoffset = reference.utcoffset()
            dst = reference.dst()
            offset = utcoffset - dst if utcoffset is not None and dst is not None else utcoffset
        return dt_timezone(offset) if offset is not None else tz

    @staticmethod
    def _get_timezone(timezone: tzinfo | str | None = None) -> tzinfo:
        """
        If timezone is None return the default() timezone;
        else if timezone is tzinfo object, return it;
        else parse string to ZoneInfo instance.
        """
        if timezone is None:
            return Timezone.default()
        return Timezone.parse(timezone) if isinstance(timezone, str) else timezone

    @staticmethod
    def localtime(value: datetime | None = None, timezone: tzinfo | str | None = None) -> datetime:
        """Converts an aware datetime - now by default - to the current zone, or to ``timezone``.

        Raises:
            ValueError: ``value`` is naive.
        """
        if value is None:
            value = Timezone.now()
        elif Timezone.is_naive(value):
            raise QueryError("localtime() cannot be applied to a naive datetime")
        tz = Timezone._get_timezone(timezone)
        return value.astimezone(tz)

    @staticmethod
    def is_aware(value: datetime | time) -> bool:
        """Whether a datetime or time is aware - ``utcoffset()`` isn't None."""
        return value.utcoffset() is not None

    @staticmethod
    def is_naive(value: datetime | time) -> bool:
        """Whether a datetime or time is naive - ``utcoffset()`` is None."""
        return value.utcoffset() is None

    @staticmethod
    def _is_nonexistent_local_time(value: datetime, tz: tzinfo) -> bool:
        """Whether a naive wall clock doesn't exist in ``tz`` - skipped by a DST spring-forward.
        Converting it with its fold=0 offset to UTC and back lands on another wall clock; an
        ambiguous one comes back unchanged.

        Args:
            value: A naive datetime.
            tz: The zone.

        Returns:
            True for a nonexistent local time.
        """
        fold0 = value.replace(tzinfo=tz, fold=0)
        fold1 = value.replace(tzinfo=tz, fold=1)
        if fold0.utcoffset() == fold1.utcoffset():
            return False
        round_trip = fold0.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
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
        tz = Timezone._get_timezone(timezone)
        midnight = datetime.combine(day, time.min)
        if hasattr(tz, "localize"):
            return tz.normalize(tz.localize(midnight, is_dst=True))  # type: ignore[attr-defined]
        # fold=0 reads a wall clock inside a gap with the offset before it - converting that
        # instant back gives the wall clock right after the gap, the day's first real moment.
        aware_midnight = midnight.replace(tzinfo=tz, fold=0)
        try:
            return aware_midnight.astimezone(UTC).astimezone(tz)
        except OverflowError:
            return aware_midnight

    @staticmethod
    def make_aware(value: datetime, timezone: tzinfo | str | None = None, is_dst: bool | None = None) -> datetime:
        """Makes a naive datetime aware in ``timezone``. ``is_dst`` picks the reading of an ambiguous
        wall clock (a DST fall-back).

        Raises:
            ValueError: ``value`` isn't naive.
            NonExistentTimeError: The wall clock is skipped by a DST spring-forward.
        """
        tz = Timezone._get_timezone(timezone)
        if hasattr(tz, "localize"):
            # pytz's own localize() already raises pytz.exceptions.NonExistentTimeError (a
            # ValueError subclass) for a spring-forward gap when is_dst is None - no separate
            # check needed on this branch.
            return tz.localize(value, is_dst=is_dst)
        if Timezone.is_aware(value):
            raise QueryError(f"make_aware expects a naive datetime, got {value}")
        if Timezone._is_nonexistent_local_time(value, tz):
            raise NonExistentTimeError(
                f"{value} is a nonexistent local time in {getattr(tz, 'key', tz)} due to a DST transition"
            )
        if is_dst is None:
            return value.replace(tzinfo=tz)
        # ZoneInfo has no localize() - is_dst is a fold: 0 the earlier (daylight) reading, 1 the
        # later one.
        return value.replace(tzinfo=tz, fold=0 if is_dst else 1)

    @staticmethod
    def make_naive(value: datetime, timezone: tzinfo | str | None = None) -> datetime:
        """
        Make an aware datetime.datetime naive in a given time zone.

        :raises ValueError: when value is naive datetime
        """
        tz = Timezone._get_timezone(timezone)
        if Timezone.is_naive(value):
            raise QueryError("make_naive() cannot be applied to a naive datetime")
        return value.astimezone(tz).replace(tzinfo=None)
