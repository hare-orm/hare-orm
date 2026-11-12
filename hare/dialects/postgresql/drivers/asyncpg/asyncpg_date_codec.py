from __future__ import annotations

from typing import Any, ClassVar

import asyncpg

from hare.native.native_modules import NativeModules


class AsyncpgDateCodec:
    """The binary ``date`` codec every asyncpg connection gets: asyncpg's own, except that
    ``date.max``/``date.min`` are written as the days they are, as the Rust driver writes them -
    asyncpg writes them as ``infinity``/``-infinity``, which ``EXTRACT``, ``date_trunc`` and a
    ``__year`` filter can't read. An ``infinity`` is still read as ``date.max``/``date.min``."""

    #: The compiled codec: a date - a datetime as its date - to Postgres's binary ``date``, and back.
    encoder: ClassVar[Any] = NativeModules.rows.write_postgres_date
    decoder: ClassVar[Any] = NativeModules.rows.read_postgres_date

    @classmethod
    async def install(cls, connection: asyncpg.Connection) -> None:
        """Registers the codec on a new connection.

        Args:
            connection: The connection.
        """
        await connection.set_type_codec(
            "date", schema="pg_catalog", encoder=cls.encoder, decoder=cls.decoder, format="binary"
        )
