from __future__ import annotations

from typing import Any, ClassVar

import asyncpg

from hare.native.native_modules import NativeModules


class AsyncpgTimestamptzCodec:
    """The binary ``timestamptz`` codec every asyncpg connection gets: asyncpg's own, except that
    ``datetime.max``/``datetime.min`` are written as the moments they are, as the Rust driver writes
    them - asyncpg writes them as ``infinity``/``-infinity``, which ``EXTRACT``, ``date_trunc`` and a
    ``__year`` filter can't read. A moment is read as an aware datetime in UTC, an ``infinity`` as
    the naive ``datetime.max``/``datetime.min``, as asyncpg reads them."""

    #: The compiled codec: a datetime to Postgres's binary ``timestamptz``, and back.
    encoder: ClassVar[Any] = NativeModules.rows.write_postgres_timestamptz
    decoder: ClassVar[Any] = NativeModules.rows.read_postgres_timestamptz

    @classmethod
    async def install(cls, connection: asyncpg.Connection) -> None:
        """Registers the codec on a new connection.

        Args:
            connection: The connection.
        """
        await connection.set_type_codec(
            "timestamptz", schema="pg_catalog", encoder=cls.encoder, decoder=cls.decoder, format="binary"
        )
