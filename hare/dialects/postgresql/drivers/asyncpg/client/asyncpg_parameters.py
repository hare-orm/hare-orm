from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

import asyncpg

from hare.dialects.postgresql.drivers.asyncpg.constants import (
    EPOCH_YEAR,
    FAST_SCALAR_BIND_TYPES,
    PLAIN_BIND_TYPE_TUPLE,
)
from hare.dialects.postgresql.drivers.asyncpg.pre_epoch_local_zone import PreEpochLocalZone
from hare.fields.constants import NAIVE_INFINITY_DATETIMES
from hare.native.native_modules import NativeModules
from hare.time.system_zone import SystemZone


class AsyncpgParameters:
    """Adapts parameters to asyncpg codecs while preserving field conversions and nested values."""

    @staticmethod
    def get_bound_value(value: Any) -> Any:
        """Adapts a value for asyncpg: a ``Range`` becomes an ``asyncpg.Range``, the only type its
        range codec takes; a naive ``datetime.time`` gets a UTC offset - a ``TimeField`` column is
        ``TIMETZ``, and the codec rejects a naive time.
        """
        if type(value) in FAST_SCALAR_BIND_TYPES:
            if type(value) is datetime.time and value.tzinfo is None:
                return value.replace(tzinfo=datetime.UTC)
            if (
                type(value) is datetime.datetime
                and value.tzinfo is None
                and value.year < EPOCH_YEAR
                and value not in NAIVE_INFINITY_DATETIMES
            ):
                return AsyncpgParameters.get_bindable_pre_epoch_datetime(value)
            return value
        if type(value) is list:
            # Elements are adapted like scalars - a TIMETZ[] parameter (a large TimeField __in list)
            # needs the naive-time offset, an ArrayField(RangeField) value asyncpg.Range elements,
            # a nested (multi-dimensional) list the same at every level.
            for element in value:
                element_type = type(element)
                if (
                    element_type not in FAST_SCALAR_BIND_TYPES
                    or element_type is datetime.time
                    or element_type is datetime.datetime
                ):
                    return [AsyncpgParameters.get_bound_value(element) for element in value]
            return value
        if (
            hasattr(value, "lower_inc")
            and hasattr(value, "upper_inc")
            and hasattr(value, "lower")
            and hasattr(value, "upper")
        ):
            # empty= is passed on: an empty range has the same None bounds as an unbounded one.
            return asyncpg.Range(
                value.lower,
                value.upper,
                lower_inc=value.lower_inc,
                upper_inc=value.upper_inc,
                empty=getattr(value, "is_empty", False),
            )
        return value

    @staticmethod
    def get_bindable_pre_epoch_datetime(value: datetime.datetime) -> datetime.datetime:
        """A naive pre-1970 datetime asyncpg can bind to a TIMESTAMPTZ.

        asyncpg reads a naive value as the system's local time through ``datetime.astimezone()``,
        which can fail near the datetime range boundary. Such a value gets the system zone's offset
        attached here, as a ``PreEpochLocalZone`` the ``timestamp`` codec drops again.

        Args:
            value: A naive datetime before 1970.

        Returns:
            `value` itself when ``astimezone()`` can convert it, the value in the local offset otherwise.
        """
        try:
            value.astimezone()
        except (OSError, OverflowError, ValueError):
            return value.replace(tzinfo=PreEpochLocalZone(SystemZone.make_system_local_aware(value).utcoffset()))
        return value

    @staticmethod
    def get_bound_values(values: Sequence[Any]) -> list[Any]:
        """``values`` adapted for asyncpg by ``get_bound_value()`` - a row of plain scalars
        (``PLAIN_BIND_TYPES``, the usual case) as it is, checked without a call per value.

        Args:
            values: The statement's parameters.

        Returns:
            The parameters to bind.
        """
        if type(values) is not list:
            values = list(values)
        if NativeModules.rows.are_all_of_types(values, PLAIN_BIND_TYPE_TUPLE):
            return values
        return [AsyncpgParameters.get_bound_value(value) for value in values]
