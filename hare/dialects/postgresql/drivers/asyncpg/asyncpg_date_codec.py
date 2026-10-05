from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any, ClassVar

import asyncpg

from hare.dialects.postgresql.drivers.asyncpg.constants import (
    POSTGRES_DATE_INFINITY,
    POSTGRES_DATE_NEGATIVE_INFINITY,
    POSTGRES_DATE_SIZE,
    POSTGRES_EPOCH_DATE_ORDINAL,
)
from hare.native.native_modules import NativeModules


class AsyncpgDateCodec:
    """The binary ``date`` codec every asyncpg connection gets: asyncpg's own, except that
    ``date.max``/``date.min`` are written as the days they are, as the Rust driver writes them -
    asyncpg writes them as ``infinity``/``-infinity``, which ``EXTRACT``, ``date_trunc`` and a
    ``__year`` filter can't read. An ``infinity`` is still read as ``date.max``/``date.min``."""

    #: The compiled codec - None without the compiled extension.
    native_encoder: ClassVar[Any] = getattr(NativeModules.rows, "write_postgres_date", None)
    native_decoder: ClassVar[Any] = getattr(NativeModules.rows, "read_postgres_date", None)

    @staticmethod
    def encode(value: Any) -> bytes:
        """Encodes a date - a datetime as its date - as Postgres's binary ``date``.

        Args:
            value: The bound date.

        Returns:
            The binary value.

        Raises:
            TypeError: The value isn't a date.
        """
        if not isinstance(value, datetime.date):
            raise TypeError(f"expected a datetime.date or datetime.datetime instance, got {type(value).__name__!r}")
        return (value.toordinal() - POSTGRES_EPOCH_DATE_ORDINAL).to_bytes(POSTGRES_DATE_SIZE, "big", signed=True)

    @staticmethod
    def decode(value: bytes) -> datetime.date:
        """Decodes Postgres's binary ``date``.

        Args:
            value: The binary value.

        Returns:
            The date; ``date.max``/``date.min`` for an infinity.
        """
        days = int.from_bytes(value, "big", signed=True)
        if days == POSTGRES_DATE_INFINITY:
            return datetime.date.max
        if days == POSTGRES_DATE_NEGATIVE_INFINITY:
            return datetime.date.min
        return datetime.date.fromordinal(days + POSTGRES_EPOCH_DATE_ORDINAL)

    @classmethod
    def get_encoder(cls) -> Callable[[Any], bytes]:
        """The encoder - the compiled one, ``encode()`` without it."""
        return cls.native_encoder or cls.encode

    @classmethod
    def get_decoder(cls) -> Callable[[bytes], datetime.date]:
        """The decoder - the compiled one, ``decode()`` without it."""
        return cls.native_decoder or cls.decode

    @classmethod
    async def install(cls, connection: asyncpg.Connection) -> None:
        """Registers the codec on a new connection.

        Args:
            connection: The connection.
        """
        await connection.set_type_codec(
            "date", schema="pg_catalog", encoder=cls.get_encoder(), decoder=cls.get_decoder(), format="binary"
        )
