from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any, ClassVar

from clickhouse_driver import connection

from hare.core.caching.cache import Cache
from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import CLICKHOUSE_DRIVER_SETTINGS_CACHE_SIZE


class ClickhouseDriverSettings:
    """The settings clickhouse-driver sends with each statement, written as the bytes kept from the
    first statement with the same settings - the library writes every setting, three values each,
    for every statement."""

    #: The library's own writing - kept to tell it was replaced, and to write settings not kept yet.
    library_writers: ClassVar[list[Callable[[Any, Any, bool, int], None]]] = []
    #: (flags, the settings, the type of each value) -> the settings as the library writes them.
    WRITTEN_SETTINGS: ClassVar[Cache[bytes]] = Cache(
        CLICKHOUSE_DRIVER_SETTINGS_CACHE_SIZE, holds_sql=False, keyed_by_model=False
    )

    @classmethod
    def install(cls) -> None:
        """Replaces the library's writing of a statement's settings - once."""
        if cls.library_writers:
            return
        cls.library_writers.append(connection.write_settings)
        connection.write_settings = cls.write

    @staticmethod
    def write(settings: dict[str, Any] | None, buffer: Any, settings_as_strings: bool, flags: int) -> None:
        """Writes the settings as the library does.

        Args:
            settings: The settings, by name.
            buffer: The connection's output.
            settings_as_strings: Whether the server reads each setting as text.
            flags: The flags written with each setting.
        """
        library_writer = ClickhouseDriverSettings.library_writers[0]
        # A server reading typed settings has the library skip the unknown ones - with a warning
        # each time.
        if not settings or not settings_as_strings:
            library_writer(settings, buffer, settings_as_strings, flags)
            return
        values = tuple(settings.values())
        # The type tells apart values the library writes differently (True and 1).
        key = (flags, tuple(settings), values, tuple(map(type, values)))
        try:
            written = ClickhouseDriverSettings.WRITTEN_SETTINGS.get(key)
        except TypeError:
            # A value that can't be a key.
            library_writer(settings, buffer, settings_as_strings, flags)
            return
        if written is None:
            output = io.BytesIO()
            library_writer(settings, output, settings_as_strings, flags)
            written = ClickhouseDriverSettings.WRITTEN_SETTINGS[key] = output.getvalue()
        buffer.write(written)
