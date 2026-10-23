from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any, ClassVar

import asyncpg

from hare.dialects.postgresql.drivers.asyncpg.constants import (
    POSTGRES_EPOCH_UTC,
    POSTGRES_TIMESTAMP_INFINITY,
    POSTGRES_TIMESTAMP_NEGATIVE_INFINITY,
    POSTGRES_TIMESTAMP_SIZE,
)
from hare.native.native_modules import NativeModules


class AsyncpgTimestamptzCodec:
    """The binary ``timestamptz`` codec every asyncpg connection gets: asyncpg's own, except that
    ``datetime.max``/``datetime.min`` are written as the moments they are, as the Rust driver writes
    them - asyncpg writes them as ``infinity``/``-infinity``, which ``EXTRACT``, ``date_trunc`` and a
    ``__year`` filter can't read. A moment is read as an aware datetime in UTC, an ``infinity`` as
    the naive ``datetime.max``/``datetime.min``, as asyncpg reads them."""

    #: The compiled codec - None without the compiled extension.
    native_encoder: ClassVar[Any] = getattr(NativeModules.rows, "write_postgres_timestamptz", None)
    native_decoder: ClassVar[Any] = getattr(NativeModules.rows, "read_postgres_timestamptz", None)

    @staticmethod
    def encode(value: Any) -> bytes:
        """Encodes a moment as Postgres's binary ``timestamptz``.

        Args:
            value: The bound datetime - a naive one, and a date's midnight, is the machine's local
                time.

        Returns:
            Microseconds since 2000-01-01 UTC.

        Raises:
            TypeError: The value isn't a datetime or a date.
        """
        if not isinstance(value, datetime.datetime):
            if not isinstance(value, datetime.date):
                raise TypeError(
                    f"expected a datetime.date or datetime.datetime instance, got {type(value).__name__!r}"
                )
            value = datetime.datetime.combine(value, datetime.time.min)
        microseconds = (value.astimezone(datetime.UTC) - POSTGRES_EPOCH_UTC) // datetime.timedelta(microseconds=1)
        return microseconds.to_bytes(POSTGRES_TIMESTAMP_SIZE, "big", signed=True)

    @staticmethod
    def decode(value: bytes) -> datetime.datetime:
        """Decodes Postgres's binary ``timestamptz``.

        Args:
            value: The binary value.

        Returns:
            The aware datetime in UTC; the naive ``datetime.max``/``datetime.min`` for an infinity.
        """
        microseconds = int.from_bytes(value, "big", signed=True)
        if microseconds == POSTGRES_TIMESTAMP_INFINITY:
            return datetime.datetime.max
        if microseconds == POSTGRES_TIMESTAMP_NEGATIVE_INFINITY:
            return datetime.datetime.min
        return POSTGRES_EPOCH_UTC + datetime.timedelta(microseconds=microseconds)

    @classmethod
    def get_encoder(cls) -> Callable[[Any], bytes]:
        """The encoder - the compiled one, ``encode()`` without it."""
        return cls.native_encoder or cls.encode

    @classmethod
    def get_decoder(cls) -> Callable[[bytes], datetime.datetime]:
        """The decoder - the compiled one, ``decode()`` without it."""
        return cls.native_decoder or cls.decode

    @classmethod
    async def install(cls, connection: asyncpg.Connection) -> None:
        """Registers the codec on a new connection.

        Args:
            connection: The connection.
        """
        await connection.set_type_codec(
            "timestamptz", schema="pg_catalog", encoder=cls.get_encoder(), decoder=cls.get_decoder(), format="binary"
        )
