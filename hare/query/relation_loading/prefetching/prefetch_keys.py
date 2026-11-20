from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from hare.dialects.base.constants import PREFETCH_BIND_PARAMETERS_HEADROOM, PREFETCH_MAX_COMPOSITE_ROWS

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class PrefetchKeys:
    """Tuple keys and driver layouts shared by direct, reverse and through-table fetches."""

    @staticmethod
    def get_value(obj: Model, names: Sequence[str]) -> tuple[Any, ...]:
        return tuple(getattr(obj, name) for name in names)

    @classmethod
    def get_distinct_values(cls, objs: Iterable[Model], names: Sequence[str]) -> list[tuple[Any, ...]]:
        """Retains first-seen order and excludes keys whose NULL component references no row."""
        return list(dict.fromkeys(key for obj in objs if None not in (key := cls.get_value(obj, names))))

    @staticmethod
    def get_batch_size(connection: DatabaseClient, width: int) -> int:
        """Bounds key batches by both parameter capacity and composite expression size."""
        capacity = max(1, (connection.features.max_bind_parameters - PREFETCH_BIND_PARAMETERS_HEADROOM) // width)
        return min(capacity, PREFETCH_MAX_COMPOSITE_ROWS) if width > 1 else capacity
