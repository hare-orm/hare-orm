from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from clickhouse_driver.columns.datetimecolumn import DateTime64Column, DateTimeColumn

from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import (
    CLICKHOUSE_DRIVER_MICROSECOND_DIGITS,
    CLICKHOUSE_DRIVER_NAIVE_MOMENT_EPOCH,
    CLICKHOUSE_DRIVER_UTC_ZONE_NAMES,
)


class ClickhouseDriverMomentColumns:
    """The reading of clickhouse-driver's columns of moments, made exact. The library turns ticks
    into a moment through ``datetime.fromtimestamp()`` of a float: a ``DateTime64`` beyond 2242 comes
    out a microsecond off, and Windows refuses any moment before 1970. A ``DateTime64`` is read from
    its ticks with integers instead - faster too, for a column in UTC; a ``DateTime`` of whole seconds
    is read by the library, from its ticks only where the library fails.
    """

    #: The library's own reading of each column class.
    library_readers: ClassVar[dict[type, Callable[..., Any]]] = {}

    @classmethod
    def install(cls) -> None:
        """Replaces the reading of the library's ``DateTime64`` and ``DateTime`` columns - once."""
        if cls.library_readers:
            return
        cls.library_readers[DateTimeColumn] = DateTimeColumn.__dict__["after_read_items"]
        cls.library_readers[DateTime64Column] = DateTime64Column.__dict__["after_read_items"]
        DateTimeColumn.after_read_items = cls.read_whole_seconds
        DateTime64Column.after_read_items = cls.read_moments

    @staticmethod
    def read_whole_seconds(column: Any, items: Sequence[int], nulls_map: Sequence[int] | None = None) -> Any:
        """A ``DateTime`` column read by the library - from its ticks where Windows refuses a moment
        before 1970."""
        try:
            return ClickhouseDriverMomentColumns.library_readers[DateTimeColumn](column, items, nulls_map)
        except (OSError, OverflowError, ValueError):
            return ClickhouseDriverMomentColumns.read_moments(column, items, nulls_map)

    @staticmethod
    def read_moments(column: Any, items: Sequence[int], nulls_map: Sequence[int] | None = None) -> tuple[Any, ...]:
        """The moments of a column's ticks, as the library gives them: aware in the column's zone,
        or the zone's wall clock for a column of naive moments.

        Args:
            column: The library's column - its ``scale`` (0 for ``DateTime``), ``timezone`` and
                ``offset_naive``.
            items: The ticks since 1970.
            nulls_map: Which items are NULL.

        Returns:
            The moments.
        """
        zone = column.timezone
        scale = getattr(column, "scale", 0)
        if scale == CLICKHOUSE_DRIVER_MICROSECOND_DIGITS:
            microseconds = list(items)
        else:
            # Finer ticks are cut to whole microseconds, as datetime keeps them.
            multiplier = 10 ** max(0, CLICKHOUSE_DRIVER_MICROSECOND_DIGITS - scale)
            divisor = 10 ** max(0, scale - CLICKHOUSE_DRIVER_MICROSECOND_DIGITS)
            microseconds = [item * multiplier // divisor for item in items]
        timedelta = datetime.timedelta
        if zone is None or str(zone) in CLICKHOUSE_DRIVER_UTC_ZONE_NAMES:
            # A moment in UTC is the epoch in UTC and the ticks after it.
            epoch = (
                CLICKHOUSE_DRIVER_NAIVE_MOMENT_EPOCH
                if column.offset_naive
                else CLICKHOUSE_DRIVER_NAIVE_MOMENT_EPOCH.replace(tzinfo=zone or datetime.UTC)
            )
            moments: list[Any] = [epoch + timedelta(microseconds=value) for value in microseconds]
        else:
            utc_epoch = CLICKHOUSE_DRIVER_NAIVE_MOMENT_EPOCH.replace(tzinfo=datetime.UTC)
            moments = [(utc_epoch + timedelta(microseconds=value)).astimezone(zone) for value in microseconds]
            if column.offset_naive:
                moments = [moment.replace(tzinfo=None) for moment in moments]
        if nulls_map is not None:
            moments = [None if is_null else moment for moment, is_null in zip(moments, nulls_map, strict=True)]
        return tuple(moments)
