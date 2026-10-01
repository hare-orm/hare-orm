import datetime
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import (
    SQLITE_LOCAL_NOW_FUNCTION_NAME,
)
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.fields.enums import NowValueType
from hare.utils import Timezone


class SqliteLocalNow:
    """Backs ``SQLITE_LOCAL_NOW_FUNCTION_NAME`` - the current date or wall-clock time in a zone
    with DST rules, which SQLite's own date functions can't apply (a ``Now()`` default of a
    ``DateField``/``TimeField``)."""

    @staticmethod
    def get_local_now_text(zone_name: str, value_type: str) -> str:
        """The current date or time in ``zone_name``, as the text a Python-side value is stored as.

        Args:
            zone_name: The IANA zone name.
            value_type: ``"date"`` or ``"time"``.

        Returns:
            ``YYYY-MM-DD``, or ``HH:MM:SS[.ffffff]`` with the zone's standard offset.
        """
        zone = Timezone.parse(zone_name)
        local_now = datetime.datetime.now(tz=datetime.UTC).astimezone(zone)
        if value_type == NowValueType.DATE:
            return local_now.date().isoformat()
        return local_now.time().replace(tzinfo=Timezone.get_fixed_offset(zone)).isoformat()

    @classmethod
    async def install(cls, connection: aiosqlite.Connection) -> None:
        """Registers the function on ``connection``.

        Args:
            connection: The SQLite connection to register the function on.
        """
        native_functions = SqliteNativeFunctions.module
        get_local_now_text: Any = cls.get_local_now_text
        if native_functions is not None:
            get_local_now_text = native_functions.LocalNow(Timezone.parse, cls.get_local_now_text).get_local_now_text
        await connection.create_function(SQLITE_LOCAL_NOW_FUNCTION_NAME, 2, get_local_now_text)
