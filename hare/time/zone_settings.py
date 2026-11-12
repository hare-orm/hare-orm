from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import tzinfo
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar
from zoneinfo import ZoneInfo

from hare.core.caching.cache_bucket import CacheBucket
from hare.time.constants import DEFAULT_TIMEZONE, NAIVE_DATETIMES_ZONE_KEY_MARK
from hare.time.system_zone import SystemZone
from hare.time.zone_names import ZoneNames

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext


class ZoneSettings:
    """The zone settings of the active context - ``use_timezone``, the configured zone and the zone
    ``override()`` sets for a block of code - and what queries read of them."""

    #: Keeps the owner bucket of ``ZONES`` below.
    cache_buckets: ClassVar[dict[int, Any]] = {}

    #: A context's configured zone name -> its zone - the owner bucket of ``ZONES`` read for every
    #: datetime assigned; taken on first use. The names are the few a configuration gives.
    configured_zones: ClassVar[CacheBucket[ZoneInfo] | None] = None

    #: The zone set for a block of code with ``override()``; None for Hare's configured zone.
    current_override: ClassVar[ContextVar[Mapping[object | None, str]]] = ContextVar(
        "hare_timezone_override", default=MappingProxyType({})
    )

    #: ``hare.core.hare_context.HareContext``, bound on first use by ``get_current_context()`` - that
    #: package imports this module, so importing it here at module level would be circular.
    context_class: ClassVar[type[HareContext] | None] = None

    @staticmethod
    def get_use_timezone() -> bool:
        """
        ``use_timezone`` of the active context - True without one.
        """
        context = ZoneSettings.get_current_context()
        return context._use_timezone if context is not None else True

    @staticmethod
    def get_settings_zone_name(context: HareContext | None) -> str:
        """The zone a context's settings name: the one ``override()`` sets for a block of code, else the
        context's configured one, else the default.

        Args:
            context: The active context, None without one.

        Returns:
            The IANA zone name.
        """
        override = ZoneSettings.current_override.get().get(None if context is None else context.identity)
        if override is not None:
            return override
        return context._timezone if context is not None else DEFAULT_TIMEZONE

    @staticmethod
    def name() -> str:
        """The current zone's name: the one ``override()`` sets for a block of code, else the
        active HareContext's configured one, else the default.

        Returns:
            The IANA zone name.
        """
        return ZoneSettings.get_settings_zone_name(ZoneSettings.get_current_context())

    @staticmethod
    def get_aware_zone_name() -> str | None:
        """The zone aware datetimes are read and written in: ``name()`` under ``use_timezone``, else None -
        one lookup of the active HareContext for both.

        Returns:
            The IANA zone name, or None when datetimes are naive.
        """
        context = ZoneSettings.get_current_context()
        if context is not None and not context._use_timezone:
            return None
        return ZoneSettings.get_settings_zone_name(context)

    @staticmethod
    def get_aware_zone() -> tzinfo | None:
        """The zone aware datetimes are read and written in - the zone of ``get_aware_zone_name()``, kept
        by its name.

        Returns:
            The zone, or None when datetimes are naive.
        """
        zone_name = ZoneSettings.get_aware_zone_name()
        if zone_name is None:
            return None
        configured_zones = ZoneSettings.configured_zones
        if configured_zones is None:
            configured_zones = ZoneSettings.configured_zones = ZoneNames.ZONES.get_bucket(ZoneSettings)
        zone = configured_zones.get(zone_name)
        if zone is None:
            zone = configured_zones[zone_name] = ZoneNames.parse(zone_name)
        return zone

    @staticmethod
    def get_rendered_zone_key() -> str | tuple[str, str | None]:
        """What a query's SQL text depends on of the zone settings: ``name()`` under
        ``use_timezone`` (a ``field__year`` lookup extracts in it); without it the machine's own
        zone (a datetime expression is converted from it), marked as that one - the two modes
        render the same zone's name differently. The machine's zone is None when it can't be known
        (no ``tzlocal``), and a query rendering it fails anyway.

        Returns:
            The IANA zone name, or the mark of naive datetimes with the machine's zone name.
        """
        context = ZoneSettings.get_current_context()
        if context is not None and not context._use_timezone:
            tzlocal = SystemZone.get_tzlocal()
            return (NAIVE_DATETIMES_ZONE_KEY_MARK, tzlocal.get_localzone_name() if tzlocal is not None else None)
        return ZoneSettings.get_settings_zone_name(context)

    @staticmethod
    def get_current_context() -> HareContext | None:
        """The active HareContext - read on every datetime conversion; its ``use_timezone`` and
        ``timezone`` are read without their properties' calls.

        Returns:
            The context, or None when none is active.
        """
        return (ZoneSettings.context_class or ZoneSettings.get_context_class()).get_current()

    @staticmethod
    def get_context_class() -> type[HareContext]:
        """``HareContext``, imported on the first read and kept in ``context_class``.

        Returns:
            The class.
        """
        # Imported here: the context module imports this one.
        from hare.core.hare_context import HareContext

        ZoneSettings.context_class = HareContext
        return HareContext

    @staticmethod
    @contextmanager
    def override(zone: str | tzinfo) -> Generator[None]:
        """Makes ``zone`` the current zone for a block of code - and the tasks it starts - in place
        of the configured one, like Django's ``timezone.override()``: the date and time parts of a
        datetime (``__date``, ``__year``, ``__hour`` lookups, ``Extract*``, ``Trunc*``) are taken in
        it, and a naive datetime is read in it. Datetimes still come back as the same moments::

            with ZoneSettings.override("Europe/Moscow"):
                today = await Book.objects.filter(created_at__date=moscow_today).count()

        Args:
            zone: An IANA zone name or a ``ZoneInfo``.

        Raises:
            ConfigurationError: The zone isn't a known IANA zone.
        """
        context = ZoneSettings.get_current_context()
        application = None if context is None else context.identity
        token = ZoneSettings.current_override.set(
            MappingProxyType({**ZoneSettings.current_override.get(), application: ZoneNames.get_zone_name(zone)})
        )
        try:
            yield
        finally:
            ZoneSettings.current_override.reset(token)
