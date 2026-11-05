from __future__ import annotations

import os
from collections.abc import Iterator, MutableMapping
from itertools import count
from threading import RLock
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast
from weakref import WeakValueDictionary

from hare.core.caching.cache_bucket import CacheBucket
from hare.core.caching.caches import Caches
from hare.core.caching.constants import (
    DEFAULT_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL,
    ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL,
    MAX_PLAIN_BUCKET_ENTRIES,
    MAX_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL,
    MIN_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL,
)
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.models import Model

TCacheValue = TypeVar("TCacheValue")


class Cache(MutableMapping[tuple[Any, ...], TCacheValue]):
    """Registered bounded storage with explicit lifetime holders.

    The mapping interface addresses global entries only. Use get_bucket(holder)
    or get_model_bucket(model) for values whose lifetime follows an object. Those
    buckets stay on their holders; the registry keeps only weak references to
    buckets, so a cached value may refer back to its model without retaining it.

    All entries use CacheBucket, including shared storage. Invalidating a cache
    clears retained buckets in place. No key is inspected to infer its lifetime.
    """

    __eq__ = object.__eq__
    __hash__ = object.__hash__
    holder_lock: ClassVar[RLock] = RLock()
    bucket_ids: ClassVar[Iterator[int]] = count()

    def __init__(
        self,
        max_size: int = 0,
        holds_sql: bool = True,
        depends_on_other_models: bool = False,
        dropped_with: Cache[Any] | None = None,
    ) -> None:
        self.max_size = max_size
        self.holds_sql = holds_sql
        self.depends_on_other_models = depends_on_other_models
        self.bucket_id = next(Cache.bucket_ids)
        self.global_bucket: CacheBucket[TCacheValue] = CacheBucket(max_size)
        self.buckets: WeakValueDictionary[int, CacheBucket[TCacheValue]] = WeakValueDictionary()
        self.dependent_caches: list[Cache[Any]] = []
        self.lock = RLock()
        if dropped_with is not None:
            dropped_with.dependent_caches.append(self)
        Caches.register(self)

    @staticmethod
    def _get_buckets(holder: Any) -> dict[int, CacheBucket[Any]]:
        meta = getattr(holder, "_meta", None)
        if meta is not None:
            return cast("dict[int, CacheBucket[Any]]", meta.plan_cache_buckets)
        buckets = (
            vars(holder).get("cache_buckets") if isinstance(holder, type) else getattr(holder, "cache_buckets", None)
        )
        if buckets is None:
            with Cache.holder_lock:
                buckets = (
                    vars(holder).get("cache_buckets")
                    if isinstance(holder, type)
                    else getattr(holder, "cache_buckets", None)
                )
                if buckets is None:
                    buckets = holder.cache_buckets = {}
        return cast("dict[int, CacheBucket[Any]]", buckets)

    def get_bucket(self, holder: Any) -> CacheBucket[TCacheValue]:
        """Get bounded storage attached to this lifetime holder."""
        buckets = self._get_buckets(holder)
        with self.lock:
            bucket = buckets.get(self.bucket_id)
            if bucket is None:
                bucket = buckets[self.bucket_id] = self.new_shared_bucket()
            return bucket

    def new_shared_bucket(self) -> CacheBucket[TCacheValue]:
        """Create storage retained by its callers and weakly tracked for invalidation."""
        bucket: CacheBucket[TCacheValue] = CacheBucket(self.max_size or MAX_PLAIN_BUCKET_ENTRIES)
        with self.lock:
            self.buckets[id(bucket)] = bucket
        return bucket

    def get_model_bucket(self, model: type[Model]) -> CacheBucket[TCacheValue]:
        """Get storage bound to a model and, for SQL, its current query builder."""
        bucket = self.get_bucket(model)
        if self.holds_sql:
            bucket.bind(model._meta.query_class)
        return bucket

    @staticmethod
    def clear_holder(holder: Any) -> None:
        # Snapshot the handles before clearing: another cache can attach a bucket.
        for bucket in tuple(Cache._get_buckets(holder).values()):
            bucket.clear()

    def get_value(self, holder: Any, default: Any = None) -> Any:
        return self.get_bucket(holder).get(None, default)

    def set_value(self, holder: Any, value: TCacheValue) -> None:
        self.get_bucket(holder)[None] = value

    def forget_holder(self, holder: Any) -> None:
        bucket = self._get_buckets(holder).get(self.bucket_id)
        if bucket is not None:
            bucket.clear()

    def get(self, key: tuple[Any, ...], default: Any = None) -> Any:
        return self.global_bucket.get(key, default)

    def __getitem__(self, key: tuple[Any, ...]) -> TCacheValue:
        return self.global_bucket[key]

    def __setitem__(self, key: tuple[Any, ...], value: TCacheValue) -> None:
        self.global_bucket[key] = value

    def __delitem__(self, key: tuple[Any, ...]) -> None:
        del self.global_bucket[key]

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self.global_bucket)

    def __len__(self) -> int:
        return len(self.global_bucket)

    @property
    def entry_count(self) -> int:
        """Count global and currently live lifetime entries for diagnostics."""
        with self.lock:
            buckets = tuple(self.buckets.values())
        return self.global_bucket.entry_count + sum(bucket.entry_count for bucket in buckets)

    def clear(self) -> None:
        for dependent_cache in self.dependent_caches:
            dependent_cache.clear()
        self.global_bucket.clear()
        with self.lock:
            buckets = tuple(self.buckets.values())
        for bucket in buckets:
            bucket.clear()

    def forget_model(self, model: type[Model]) -> None:
        if self.depends_on_other_models:
            self.clear()
            return
        for dependent_cache in self.dependent_caches:
            dependent_cache.clear()
        self.forget_holder(model)

    def forget_all(self) -> None:
        self.clear()

    @staticmethod
    def max_size_from_env() -> int:
        """The bucket size of the statement plan caches, from
        ``ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL`` or its default when unset.

        Raises:
            ConfigurationError: The variable holds no whole number within the bounds.
        """
        configured_size = os.environ.get(ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL)
        if configured_size is None:
            return DEFAULT_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL
        try:
            size = int(configured_size)
        except ValueError:
            size = None
        if (
            size is None
            or not MIN_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL <= size <= MAX_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL
        ):
            raise ConfigurationError(
                f"{ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL}={configured_size!r} is not a whole number from "
                f"{MIN_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL} to {MAX_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL}"
            )
        return size
