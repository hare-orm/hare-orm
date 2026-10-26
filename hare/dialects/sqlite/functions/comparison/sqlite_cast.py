from __future__ import annotations

import datetime
from typing import Any

import aiosqlite

from hare.dialects.sqlite.constants import SQLITE_CAST_FUNCTION_NAME
from hare.dialects.sqlite.functions.sqlite_function_errors import SqliteFunctionErrors
from hare.sql.enums import CastType
from hare.time import Timezone


class SqliteCast:
    """Backs ``SQLITE_CAST_FUNCTION_NAME`` - ``Cast()`` on SQLite to a date, a datetime or a time of day,
    read from its stored text in UTC. A number, a boolean and text are SQLite's own ``CAST``."""

    @staticmethod
    def get_moment(value: Any) -> datetime.datetime:
        """A stored date/datetime text as a UTC moment - no offset means UTC."""
        moment = datetime.datetime.fromisoformat(str(value).strip())
        if moment.tzinfo is None:
            return moment.replace(tzinfo=datetime.UTC)
        return moment.astimezone(datetime.UTC)

    @staticmethod
    def get_wall_clock(moment: datetime.datetime) -> datetime.datetime:
        """A naive timestamp's wall clock - an aware one in the machine's local zone."""
        return Timezone.get_system_local_naive(moment) if moment.tzinfo is not None else moment

    @classmethod
    def get_naive_moment(cls, value: Any, source: str) -> datetime.datetime:
        """A date/datetime text as a naive wall clock - a text's own offset ignored."""
        moment = datetime.datetime.fromisoformat(str(value).strip())
        if source == CastType.DATETIME:
            return cls.get_wall_clock(moment)
        return moment.replace(tzinfo=None)

    @classmethod
    def cast(cls, value: Any, target: str, source: str, is_aware: int) -> str | None:
        """Backs ``SQLITE_CAST_FUNCTION_NAME`` - a date, datetime or time of day, read in UTC; a naive
        timestamp as its wall clock.

        Args:
            value: The value.
            target: The ``CastType`` converted to - a date, a datetime or a time of day.
            source: The ``CastType`` of the value, or ``unknown``.
            is_aware: Whether timestamps are aware - a naive one converts as its wall clock.

        Returns:
            The converted value, or ``None`` for NULL.

        Raises:
            ValueError: A number, or text that isn't a date/time.
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            raise ValueError(f"{value!r} is no {target}")
        text = str(value).strip()
        if target == CastType.TIME:
            if source == CastType.DATETIME:
                moment = cls.get_moment(text) if is_aware else cls.get_naive_moment(text, source)
                return moment.time().isoformat()
            try:
                return datetime.time.fromisoformat(text).replace(tzinfo=None).isoformat()
            except ValueError:
                pass
            # A timestamp's text gives its own clock digits, whatever its offset; a date its midnight.
            return datetime.datetime.fromisoformat(text).time().replace(tzinfo=None).isoformat()
        if target == CastType.DATE and source != CastType.DATETIME:
            # A text's date as it is written, whatever time and offset follow it.
            return datetime.datetime.fromisoformat(text).date().isoformat()
        moment = cls.get_moment(text) if is_aware else cls.get_naive_moment(text, source)
        if target == CastType.DATE:
            return moment.date().isoformat()
        return moment.isoformat(" ")

    @classmethod
    async def install(cls, connection: aiosqlite.Connection, function_errors: SqliteFunctionErrors) -> None:
        """Registers ``cast`` on ``connection`` as ``SQLITE_CAST_FUNCTION_NAME``.

        Args:
            connection: The SQLite connection.
            function_errors: The connection's function errors.
        """
        await connection.create_function(
            SQLITE_CAST_FUNCTION_NAME, 4, function_errors.wrap(cls.cast), deterministic=True
        )
