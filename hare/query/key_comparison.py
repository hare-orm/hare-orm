from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field


class KeyComparison:
    """Use the compared columns' database equality to match keys in Python."""

    @staticmethod
    def needs_conversion(connection: DatabaseClient, fields: Iterable[Field[Any]]) -> bool:
        return any(connection.dialect.types.get_comparison_key(field) is not None for field in fields)

    @staticmethod
    def unchanged(key: tuple[Any, ...]) -> tuple[Any, ...]:
        return key

    @classmethod
    async def get_key_reader(
        cls,
        connection: DatabaseClient,
        fields: Sequence[Field[Any]],
        keys: Iterable[tuple[Any, ...]],
    ) -> Callable[[tuple[Any, ...]], tuple[Any, ...]]:
        """Return a stable comparison key for values from both sides of one match."""
        types = connection.dialect.types
        factories = [
            (position, factory)
            for position, field in enumerate(fields)
            if (factory := types.get_comparison_key(field)) is not None
        ]
        if not factories:
            return cls.unchanged
        candidates = tuple(keys)
        readers = [
            (position, await factory(connection, (key[position] for key in candidates)))
            for position, factory in factories
        ]

        def read(key: tuple[Any, ...]) -> tuple[Any, ...]:
            values = list(key)
            for position, read_value in readers:
                values[position] = read_value(values[position])
            return tuple(values)

        return read
