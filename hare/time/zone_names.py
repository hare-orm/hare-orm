from __future__ import annotations

from datetime import tzinfo
from typing import ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from hare.core.caching.cache import Cache
from hare.exceptions import ConfigurationError
from hare.time.constants import DEFAULT_TIMEZONE, ZONE_CACHE_SIZE


class ZoneNames:
    """IANA zone names: a name's zone (kept for the next call), its spelling in the zone database, and the
    check of a configured name."""

    #: (zone name,) -> its zone.
    ZONES: ClassVar[Cache[ZoneInfo]] = Cache(ZONE_CACHE_SIZE)

    @staticmethod
    def parse(zone: str) -> ZoneInfo:
        """The time zone of a name, kept for the next call - spelled as the zone database spells it.

        Args:
            zone: The zone's name, in any case.

        Returns:
            The zone.

        Raises:
            ZoneInfoNotFoundError: No zone has the name.
        """
        cache_key = DEFAULT_TIMEZONE if zone == DEFAULT_TIMEZONE or zone.upper() == DEFAULT_TIMEZONE else zone
        cached = ZoneNames.ZONES.get((cache_key,))
        if cached is not None:
            return cached
        # Spelled as the zone database spells it, whatever the case it is given in (US/central is
        # US/Central) - a database reading the name takes it then.
        zone_info = ZoneInfo(ZoneNames.get_zone_key(cache_key))
        ZoneNames.ZONES[(cache_key,)] = zone_info
        return zone_info

    @staticmethod
    def get_zone_key(zone: str) -> str:
        """The name of a zone as the zone database spells it.

        Args:
            zone: The name, in any case.

        Returns:
            The database's name.

        Raises:
            ZoneInfoNotFoundError: The database has no zone of the name - a file name the file system
                would read as one (a trailing space, a backslash) included.
        """
        if zone == DEFAULT_TIMEZONE:
            return zone
        zone_keys = {name.lower(): name for name in available_timezones()}
        key = zone_keys.get(zone.lower())
        if key is None:
            raise ZoneInfoNotFoundError(f"No time zone found with key {zone}")
        return key

    @staticmethod
    def validate(zone: str) -> None:
        """Checks that a configured timezone name is a known IANA zone.

        Args:
            zone: The configured timezone.

        Raises:
            ConfigurationError: If the zone is unknown.
        """
        if zone.upper() == DEFAULT_TIMEZONE:
            # Always known - the zone database ships with the system or with hare's tzdata
            # dependency. Its zone data is read on first use: loading it through tzdata
            # costs a few milliseconds of every Hare.init().
            return
        try:
            ZoneNames.parse(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigurationError(f"Unknown timezone {zone!r}: expected an IANA zone name like 'UTC'") from None

    @staticmethod
    def get_zone_name(zone: str | tzinfo) -> str:
        """The IANA name of a zone given by name or as a ``ZoneInfo``.

        Args:
            zone: The zone.

        Returns:
            Its IANA name, as the zone database spells it.

        Raises:
            ConfigurationError: The zone is unknown, or a tzinfo without an IANA name (a fixed
                offset) - the database converts by name.
        """
        if isinstance(zone, str):
            ZoneNames.validate(zone)
            return ZoneNames.parse(zone).key
        zone_key = getattr(zone, "key", None)
        if isinstance(zone_key, str):
            return zone_key
        raise ConfigurationError(f"A time zone must be an IANA zone name or a ZoneInfo, got {zone!r}")
