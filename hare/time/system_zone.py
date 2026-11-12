from __future__ import annotations

from datetime import UTC, datetime, timezone as dt_timezone, tzinfo
from types import ModuleType
from typing import ClassVar

from hare.exceptions import ConfigurationError
from hare.time.constants import SYSTEM_ZONE_FALLBACK_REFERENCE_MOMENT
from hare.time.zone_names import ZoneNames


class SystemZone:
    """The zone of the machine this process runs on - read through the optional ``tzlocal`` module, with a
    fallback where the system's own conversion fails - and datetimes read or written in it."""

    #: The optional ``tzlocal`` module, imported by the first ``get_tzlocal()`` rather than with
    #: hare; None without it installed.
    tzlocal: ClassVar[ModuleType | None] = None

    #: Whether ``tzlocal`` was looked for.
    tzlocal_loaded: ClassVar[bool] = False

    #: The zone of the system's local time when it can't be read - found on first use.
    system_zone_fallback: ClassVar[tzinfo | None] = None

    @staticmethod
    def get_tzlocal() -> ModuleType | None:
        """The ``tzlocal`` module, imported now when it wasn't.

        Returns:
            The module, None without it installed.
        """
        if not SystemZone.tzlocal_loaded:
            try:
                import tzlocal
            except ImportError:
                SystemZone.tzlocal = None
            else:
                SystemZone.tzlocal = tzlocal
            SystemZone.tzlocal_loaded = True
        return SystemZone.tzlocal

    @staticmethod
    def get_local_zone_name() -> str:
        """The IANA zone name of the machine this process runs on - independent of Hare's own
        configured ``use_timezone``/``timezone``.

        Raises:
            ConfigurationError: the optional ``tzlocal`` dependency isn't installed.
        """
        tzlocal = SystemZone.get_tzlocal()
        if tzlocal is None:
            raise ConfigurationError(
                "Determining the local system time zone requires the optional 'tzlocal' "
                "dependency - install it (pip install hare-orm[tzlocal]) or set use_timezone=True to "
                "avoid needing to know the local zone at all."
            )
        return tzlocal.get_localzone_name()

    @staticmethod
    def get_system_zone_fallback() -> tzinfo:
        """The system's local zone for an instant ``datetime.astimezone()`` can't convert.

        Returns:
            The IANA zone ``tzlocal`` names, or - without it - a fixed offset: the system's own
            offset at ``SYSTEM_ZONE_FALLBACK_REFERENCE_MOMENT``.
        """
        if SystemZone.system_zone_fallback is None:
            SystemZone.system_zone_fallback = SystemZone.find_system_zone_fallback()
        return SystemZone.system_zone_fallback

    @staticmethod
    def find_system_zone_fallback() -> tzinfo:
        """The zone ``get_system_zone_fallback()`` keeps."""
        tzlocal = SystemZone.get_tzlocal()
        if tzlocal is not None:
            try:
                return ZoneNames.parse(tzlocal.get_localzone_name())
            except (LookupError, ValueError, OSError):
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
        except (OSError, OverflowError, ValueError):
            local_value = value.astimezone(SystemZone.get_system_zone_fallback())
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
        except (OSError, OverflowError, ValueError):
            return value.replace(tzinfo=SystemZone.get_system_zone_fallback())
