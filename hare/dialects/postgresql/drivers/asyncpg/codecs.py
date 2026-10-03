import datetime

import asyncpg

from hare.dialects.postgresql.drivers.asyncpg.constants import (
    POSTGRES_EPOCH,
    POSTGRES_TIMESTAMP_INFINITY,
    POSTGRES_TIMESTAMP_NEGATIVE_INFINITY,
)
from hare.fields.data.temporal.time_delta_field import TimeDeltaField


class AsyncpgTimestampCodec:
    """The ``timestamp`` (no time zone) codec every asyncpg connection gets: an aware datetime is
    written as its UTC wall-clock time - the conversion Postgres itself applies in hare's UTC
    session, and the one the Rust driver makes - instead of being rejected. A naive value is
    written and read back as is."""

    @staticmethod
    def encode(value: datetime.date) -> tuple[int]:
        """Encodes a datetime as Postgres's binary ``timestamp``.

        Args:
            value: The bound datetime, or a date (its midnight).

        Returns:
            Microseconds since 2000-01-01, or an infinity for ``datetime.max``/``datetime.min``.

        Raises:
            TypeError: The value isn't a datetime or a date.
        """
        if not isinstance(value, datetime.datetime):
            if not isinstance(value, datetime.date):
                raise TypeError(f"expected a datetime.datetime instance, got {type(value).__name__!r}")
            value = datetime.datetime.combine(value, datetime.time.min)
        if value.tzinfo is not None and value.utcoffset() is not None:
            value = value.astimezone(datetime.UTC).replace(tzinfo=None)
        else:
            value = value.replace(tzinfo=None)
        if value == datetime.datetime.max:
            return (POSTGRES_TIMESTAMP_INFINITY,)
        if value == datetime.datetime.min:
            return (POSTGRES_TIMESTAMP_NEGATIVE_INFINITY,)
        return (TimeDeltaField.get_microseconds(value - POSTGRES_EPOCH),)

    @staticmethod
    def decode(value: tuple[int]) -> datetime.datetime:
        """Decodes Postgres's binary ``timestamp`` into a naive datetime, as asyncpg itself does.

        Args:
            value: Microseconds since 2000-01-01.

        Returns:
            The naive datetime; ``datetime.max``/``datetime.min`` for an infinity.
        """
        microseconds = value[0]
        if microseconds == POSTGRES_TIMESTAMP_INFINITY:
            return datetime.datetime.max
        if microseconds == POSTGRES_TIMESTAMP_NEGATIVE_INFINITY:
            return datetime.datetime.min
        return POSTGRES_EPOCH + datetime.timedelta(microseconds=microseconds)

    @classmethod
    async def install(cls, connection: asyncpg.Connection) -> None:
        """Registers the codec on a new connection.

        Args:
            connection: The connection.
        """
        await connection.set_type_codec(
            "timestamp", schema="pg_catalog", encoder=cls.encode, decoder=cls.decode, format="tuple"
        )
