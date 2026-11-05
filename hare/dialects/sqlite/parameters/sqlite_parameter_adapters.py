from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from hare.dialects.sqlite.parameters.constants import SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
from hare.dialects.sqlite.parameters.sqlite_bound_parameter import SqliteBoundParameter
from hare.dialects.sqlite.parameters.sqlite_encoded_text import SqliteEncodedText


@dataclass(frozen=True, slots=True, eq=False, init=False)
class SqliteParameterAdapters:
    """An immutable client-local snapshot of Python-to-storage conversions.

    Adapters match exact Python types, run once, and return native SQLite storage values.
    Neither constructing a snapshot nor preparing values reads or modifies sqlite3.adapters.
    """

    adapters: Mapping[type, Callable[[Any], Any]]

    def __init__(self, adapters: Mapping[type, Callable[[Any], Any]] | None = None) -> None:
        conversions = (
            dict(adapters)
            if adapters is not None
            else {
                Decimal: self.adapt_decimal,
                datetime.date: datetime.date.isoformat,
                datetime.datetime: self.adapt_datetime,
                datetime.time: datetime.time.isoformat,
            }
        )
        object.__setattr__(self, "adapters", MappingProxyType(conversions))

    def with_adapter(self, value_type: type, adapter: Callable[[Any], Any]) -> SqliteParameterAdapters:
        """Create a snapshot with one replacement, leaving active users unchanged."""
        return type(self)({**self.adapters, value_type: adapter})

    def adapt(self, value: Any) -> Any:
        """Convert one input exactly once, preserving SQLite's public conform protocol.

        Internal packed text is already encoded and bypasses user adapters. Unknown values
        are returned unchanged so the driver reports its ordinary unsupported-type error.
        """
        if isinstance(value, SqliteEncodedText):
            return str(value)
        adapter = self.adapters.get(type(value))
        if adapter is not None:
            return adapter(value)
        conform = getattr(value, "__conform__", None)
        if conform is not None:
            import sqlite3

            converted = conform(sqlite3.PrepareProtocol)
            if converted is not None:
                return converted
        return value

    def bind(self, values: list[Any] | None) -> list[SqliteBoundParameter | None]:
        """Prepare positional values before handing them to the driver's worker thread."""
        prepared = [self.adapt(value) for value in values or ()]
        return [None if value is None else SqliteBoundParameter(value) for value in prepared]

    @staticmethod
    def adapt_decimal(value: Decimal) -> str:
        """Decimal text in fixed-point notation - SQLite has no decimal type to bind one as.

        Args:
            value: The bound decimal.

        Returns:
            ``0.0000000001`` rather than ``1E-10``; scientific text for a non-finite value or an
            exponent past ``SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT``. A zero without its sign -
            ``-0.00`` is ``0.00``.
        """
        if value.is_zero():
            value = value.copy_abs()
        if (
            value.is_finite()
            and value.adjusted() <= SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
            and int(value.as_tuple().exponent) >= -SQLITE_DECIMAL_FIXED_POINT_MAX_EXPONENT
        ):
            return format(value, "f")
        return str(value)

    @staticmethod
    def adapt_datetime(value: datetime.datetime) -> str:
        """ISO text of a datetime, an aware one converted to UTC first.

        SQLite has no ``TIMESTAMPTZ``: ordering, comparisons and ``Min``/``Max`` compare the stored
        text, which only matches chronological order when every row carries the same offset.

        Args:
            value: The bound datetime.

        Returns:
            ``2024-06-01 08:00:00+00:00`` for an aware value, the naive wall-clock text otherwise.
        """
        if value.tzinfo is not None:
            value = value.astimezone(datetime.UTC)
        return value.isoformat(" ")
