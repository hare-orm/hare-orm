from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from threading import RLock
from typing import Any, Generic, TypeVar, cast

from hare.core.constants import CACHE_MISS
from hare.core.registries import Registries

TValue = TypeVar("TValue")


class CacheBucket(MutableMapping[Any, TValue], Generic[TValue]):
    """Lifetime-bound LRU storage; its holder retains values, the registry only a weak reference.

    All writes obey max_size, including update() and setdefault(). Clearing preserves the
    bucket identity, so a query and its clones cannot retain a detached stale dictionary.
    Generation belongs to the bucket and changes whenever its contents are invalidated.
    Entries are scoped to the execution's registration snapshot. One LRU bound covers
    all snapshots, including an older execution continuing after a publication.
    Individual reads, writes and invalidations are synchronized across threads; a
    caller's multi-step computation is not a cache transaction.
    """

    def __init__(self, max_size: int) -> None:
        self.max_size = max_size
        self.generation = 0
        self.context: Any = None
        self.entries: OrderedDict[Any, TValue] = OrderedDict()
        self.lock = RLock()

    def get(self, key: Any, default: Any = None) -> Any:
        entry_key = (Registries.current(), key)
        with self.lock:
            value = self.entries.get(entry_key, CACHE_MISS)
            if value is CACHE_MISS:
                return default
            self.entries.move_to_end(entry_key)
            return value

    def __getitem__(self, key: Any) -> TValue:
        value = self.get(key, CACHE_MISS)
        if value is CACHE_MISS:
            raise KeyError(key)
        return cast("TValue", value)

    def __setitem__(self, key: Any, value: TValue) -> None:
        if self.max_size == 0:
            return
        entry_key = (Registries.current(), key)
        with self.lock:
            self.entries[entry_key] = value
            self.entries.move_to_end(entry_key)
            if len(self.entries) > self.max_size:
                self.entries.popitem(last=False)

    def store(self, key: Any, value: TValue) -> TValue:
        """Store a computed result under the same bound as every other assignment."""
        self[key] = value
        return value

    def __delitem__(self, key: Any) -> None:
        with self.lock:
            del self.entries[Registries.current(), key]

    def __iter__(self) -> Iterator[Any]:
        # Mapping views may call __getitem__, which refreshes LRU order.
        snapshot = Registries.current()
        with self.lock:
            return iter(tuple(key for (entry_snapshot, key) in self.entries if entry_snapshot is snapshot))

    def __len__(self) -> int:
        snapshot = Registries.current()
        with self.lock:
            return sum(entry_snapshot is snapshot for entry_snapshot, _ in self.entries)

    @property
    def entry_count(self) -> int:
        """The bounded total across all executions' registration snapshots."""
        with self.lock:
            return len(self.entries)

    def bind(self, context: Any) -> None:
        """Invalidate when the explicit compilation context changes."""
        with self.lock:
            if self.context is not context:
                self.clear()
                self.context = context

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()
            self.generation += 1
