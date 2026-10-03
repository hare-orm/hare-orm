from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast
from weakref import WeakSet, WeakValueDictionary

from hare.core.caches import Caches
from hare.core.constants import (
    CACHE_MISS,
    DEFAULT_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL,
    ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL,
)
from hare.core.declarations import SharedCacheBucket

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


TCacheValue = TypeVar("TCacheValue")


class Cache(MutableMapping[tuple[Any, ...], TCacheValue]):
    """A dict-like, least-recently-used cache by tuple keys, registered in ``Caches`` when created.

    A key holding a model class goes into that model's own bucket (``MetaInfo.plan_cache_buckets``),
    which lives as long as the class; ``forget_model()`` drops it. Such an entry records the model's
    query builder class and is a miss once the model is bound to another one - unless the cache's
    values hold no SQL. A key without a model goes into the cache's own bucket. Every bucket holds
    at most ``max_size`` entries.

    Where a lookup must cost no more than a dict's, an owner - a model class, a registry - keeps a
    bucket of the cache itself (``get_owner_bucket()``) and reads it as a plain dict; the cache
    empties it in place whenever its entries are dropped. Entries that belong to no model and no
    owner - what a queryset shares with its clones - go into a shared bucket (``new_shared_bucket()``).

    Args:
        max_size: The most entries a bucket holds - 0 for a cache read through owner buckets alone.
        holds_sql: Whether the values hold SQL of the model's query builder. False for values good
            for any builder, and for a model not bound yet - a model's entries then stay until
            the model is forgotten.
        keyed_by_model: Whether a key may hold a model class. False for keys that never do - they
            are not searched for one, and every entry goes into the cache's own bucket.
        depends_on_other_models: Whether an entry describes other models than the one in its key
            too - a change to any model then drops every entry.
        model_attribute: The attribute of a model's ``_meta`` its plain bucket
            (``get_model_bucket()``) is kept in as well, None while the model has no bucket - read
            there where even asking the cache for the bucket is too much.
        owner_attribute: The attribute an owner's one value of this cache is kept in
            (``set_owner_value()``), None while it has none - for a value read on every query.
    """

    #: `hare.models.Model`, bound on the first `split()` - `hare.models` itself imports this
    #: module (indirectly, through `QuerySet`), so a module-level import here would be circular;
    #: by the time any cache is read or written, `hare.models` has long finished importing.
    model_base_class: ClassVar[type | None] = None

    # A cache is one object - two caches holding the same entries are still two caches (the
    # registry finds and removes a cache by it).
    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def __init__(
        self,
        max_size: int = 0,
        holds_sql: bool = True,
        keyed_by_model: bool = True,
        depends_on_other_models: bool = False,
        model_attribute: str | None = None,
        owner_attribute: str | None = None,
    ) -> None:
        self.max_size = max_size
        self.holds_sql = holds_sql
        self.keyed_by_model = keyed_by_model
        self.depends_on_other_models = depends_on_other_models
        self.model_attribute = model_attribute
        self.owner_attribute = owner_attribute
        #: Counts the times every entry was dropped. An owner the cache can't refer to weakly (a
        #: field) keeps its value itself, with the generation it was built in - a value kept under
        #: another generation is stale.
        self.generation = 0
        #: Whether anything may be held - False from a drop until the next entry, so dropping an
        #: empty cache again and again (every model set up drops them all) costs nothing.
        self.may_hold_entries = False
        #: The owners keeping a value of this cache in their ``owner_attribute`` - held weakly;
        #: emptied when the values are dropped.
        self.value_owners: WeakSet[Any] = WeakSet()
        #: The models holding a bucket of this cache - held weakly, so a model class dies with
        #: its bucket; used by iteration, `len()` and `clear()`.
        self.models: WeakSet[type[Model]] = WeakSet()
        #: The entries of keys without a model.
        self.own_bucket: OrderedDict[tuple[Any, ...], Any] = OrderedDict()
        #: The owners keeping a bucket of this cache (``get_owner_bucket()``) - held weakly, so an
        #: owner dies with its bucket.
        self.owners: WeakSet[Any] = WeakSet()
        #: The buckets handed out by ``new_shared_bucket()``, by ``id()`` - held weakly, so a bucket
        #: is gone when nothing holds it.
        self.shared_buckets: WeakValueDictionary[int, SharedCacheBucket] = WeakValueDictionary()
        Caches.register(self)

    @staticmethod
    def _get_buckets_of(owner: Any) -> dict[int, Any]:
        """Where ``owner`` keeps the buckets of the caches it reads - a model class on its ``_meta``,
        any other owner in its ``cache_buckets``."""
        meta = getattr(owner, "_meta", None)
        return cast("dict[int, Any]", meta.plan_cache_buckets if meta is not None else owner.cache_buckets)

    def get_owner_bucket(self, owner: Any) -> dict[Any, Any]:
        """The bucket of this cache ``owner`` keeps and reads as a plain dict, with keys of its own
        choice - for a lookup that must cost no more than a dict's. The cache empties the bucket in
        place whenever its entries are dropped (``forget_all()``; ``forget_model()`` for an owner
        that is the model, or any model when the entries depend on other models), so the owner's
        reference stays good. The bucket isn't bounded - its keys must be few by nature (classes, a
        model's field paths). A cache handing out owner buckets holds no entries keyed by a model.

        Args:
            owner: The object the entries belong to - a model class, or an object with a
                ``cache_buckets`` dict. Referenced weakly.

        Returns:
            The bucket.
        """
        buckets = self._get_buckets_of(owner)
        bucket = buckets.get(id(self))
        if bucket is None:
            bucket = buckets[id(self)] = {}
            self.owners.add(owner)
            self.may_hold_entries = True
        return cast("dict[Any, Any]", bucket)

    def new_shared_bucket(self) -> dict[Any, Any]:
        """A plain dict of this cache for entries that belong to no model and no owner object:
        whoever holds the bucket shares it (a queryset and its clones), and it is gone when none
        of them is left. The cache empties it in place whenever its entries are dropped - on
        ``forget_all()``, and on a change to any model when the entries depend on other models.
        The bucket isn't bounded.

        Returns:
            The bucket.
        """
        bucket = SharedCacheBucket()
        self.shared_buckets[id(bucket)] = bucket
        self.may_hold_entries = True
        return bucket

    def get_model_bucket(self, model: type[Model]) -> dict[Any, Any]:
        """The plain dict ``model`` keeps for this cache, with keys of the caller's choice - for a
        lookup that must cost no more than a dict's. It is dropped with the model's entries
        (``forget_model()``, ``forget_all()``), so it is asked for on every read and never kept -
        or read off the model's ``_meta`` (``model_attribute``), which is None once it is dropped.
        For a cache whose keys hold no model (``keyed_by_model=False``); not bounded - its keys
        must be few by nature (a model's field paths).

        Args:
            model: The model.

        Returns:
            The bucket.
        """
        meta = model._meta
        buckets = meta.plan_cache_buckets
        bucket = buckets.get(id(self))
        if bucket is None:
            bucket = buckets[id(self)] = {}
            self.models.add(model)
            self.may_hold_entries = True
            if self.model_attribute is not None:
                setattr(meta, self.model_attribute, bucket)
        return bucket

    def _drop_model_bucket(self, model: type[Model]) -> None:
        """Takes this cache's bucket off ``model``."""
        meta = model._meta
        meta.plan_cache_buckets.pop(id(self), None)
        if self.model_attribute is not None:
            setattr(meta, self.model_attribute, None)

    def set_owner_value(self, owner: Any, value: TCacheValue) -> None:
        """Keeps the one value ``owner`` has in this cache in the owner's ``owner_attribute`` - the
        owner reads the attribute itself, and finds None there once the cache dropped the value
        (``forget_all()``; a change to any model when the values depend on other models).

        Args:
            owner: The object the value belongs to. Referenced weakly.
            value: The value.
        """
        setattr(owner, cast("str", self.owner_attribute), value)
        self.value_owners.add(owner)
        self.may_hold_entries = True

    def _drop_owner_values(self) -> None:
        """Takes every owner's value of this cache off its owner."""
        if self.value_owners:
            attribute = cast("str", self.owner_attribute)
            for owner in list(self.value_owners):
                setattr(owner, attribute, None)
            self.value_owners.clear()

    def forget_owner(self, owner: Any) -> None:
        """Empties the bucket ``owner`` keeps.

        Args:
            owner: The owner.
        """
        bucket = self._get_buckets_of(owner).get(id(self))
        if bucket is not None:
            bucket.clear()

    @classmethod
    def split(cls, key: tuple[Any, ...]) -> tuple[type[Model] | None, int, tuple[Any, ...]]:
        """The `Model` subclass in `key`, its index, and the rest of the key without it - the key of
        the model's bucket; None and the key itself for a key without a model."""
        model_base_class = cls.model_base_class
        if model_base_class is None:
            from hare.models import Model as ModelClass

            model_base_class = cls.model_base_class = ModelClass
        for index, item in enumerate(key):
            if isinstance(item, type) and issubclass(item, model_base_class):
                return cast("type[Model]", item), index, key[:index] + key[index + 1 :]
        return None, 0, key

    def _get_bucket(self, model: type[Model]) -> OrderedDict[tuple[int, tuple[Any, ...]], Any] | None:
        """The bucket of this cache on ``model``, None when it has none."""
        return model._meta.plan_cache_buckets.get(id(self))

    def get(self, key: tuple[Any, ...], default: Any = None) -> Any:
        """The cached value, or ``default`` when there is none - one lookup, no exception.

        Args:
            key: The cache key.
            default: Returned on a miss.

        Returns:
            The value or ``default``.
        """
        model, index, rest = self.split(key) if self.keyed_by_model else (None, 0, key)
        if model is None:
            value = self.own_bucket.get(key, CACHE_MISS)
            if value is CACHE_MISS:
                return default
            self.own_bucket.move_to_end(key)
            return value
        return self.get_for_model(model, rest, default, index)

    def get_for_model(self, model: type[Model], rest: tuple[Any, ...], default: Any = None, index: int = 0) -> Any:
        """``get()`` for a key given as its model and the rest of it - nothing to locate.

        Args:
            model: The model in the key.
            rest: The key without the model.
            default: Returned on a miss.
            index: Where the model stands in the key - first, by default.

        Returns:
            The value or ``default``.
        """
        meta = model._meta
        bucket = meta.plan_cache_buckets.get(id(self))
        if bucket is None:
            return default
        bucket_key = (index, rest)
        stored = bucket.get(bucket_key)
        if stored is None:
            return default
        builder_class, value = stored
        # MetaInfo.query_builder_class, read without the property's call - every plan lookup does it.
        # None: a value without SQL, good for any builder.
        if builder_class is not type(meta.basequery) and builder_class is not None:
            del bucket[bucket_key]
            return default
        bucket.move_to_end(bucket_key)
        return value

    def __getitem__(self, key: tuple[Any, ...]) -> TCacheValue:
        value = self.get(key, CACHE_MISS)
        if value is CACHE_MISS:
            raise KeyError(key)
        return cast("TCacheValue", value)

    def __contains__(self, key: object) -> bool:
        return self.get(cast("tuple[Any, ...]", key), CACHE_MISS) is not CACHE_MISS

    def __setitem__(self, key: tuple[Any, ...], value: TCacheValue) -> None:
        self.may_hold_entries = True
        model, index, rest = self.split(key) if self.keyed_by_model else (None, 0, key)
        if model is None:
            bucket: OrderedDict[Any, Any] = self.own_bucket
            bucket[key] = value
            bucket.move_to_end(key)
        else:
            model_bucket = self._get_bucket(model)
            if model_bucket is None:
                model_bucket = model._meta.plan_cache_buckets[id(self)] = OrderedDict()
                self.models.add(model)
            bucket = model_bucket
            bucket_key = (index, rest)
            bucket[bucket_key] = (model._meta.query_builder_class if self.holds_sql else None, value)
            bucket.move_to_end(bucket_key)
        if len(bucket) > self.max_size:
            bucket.popitem(last=False)

    def __delitem__(self, key: tuple[Any, ...]) -> None:
        model, index, rest = self.split(key) if self.keyed_by_model else (None, 0, key)
        if model is None:
            del self.own_bucket[key]
            return
        bucket = self._get_bucket(model)
        bucket_key = (index, rest)
        if bucket is None or bucket_key not in bucket:
            raise KeyError(key)
        del bucket[bucket_key]

    def _live_items(self) -> Iterator[tuple[tuple[Any, ...], Any]]:
        """Every ``(key, value)`` still served - an entry written under another query builder
        than its model's current one is skipped, as `get()` would miss it."""
        yield from list(self.own_bucket.items())
        if not self.keyed_by_model:
            # The models' buckets are plain dicts with keys of their readers' own.
            return
        for model in list(self.models):
            bucket = self._get_bucket(model)
            if bucket is None:
                continue
            builder_class = model._meta.query_builder_class if self.holds_sql else None
            for (index, rest), (stored_builder_class, value) in list(bucket.items()):
                if stored_builder_class is builder_class:
                    yield rest[:index] + (model,) + rest[index:], value

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        """Yields only keys `__getitem__()` serves right after - the `Mapping` contract."""
        for key, _ in self._live_items():
            yield key

    def __len__(self) -> int:
        return len(self.own_bucket) + sum(
            len(bucket) for model in list(self.models) if (bucket := self._get_bucket(model)) is not None
        )

    def values(self) -> Iterator[TCacheValue]:  # type: ignore[override]
        for _, value in self._live_items():
            yield cast("TCacheValue", value)

    def clear(self) -> None:
        self.generation += 1
        if not self.may_hold_entries:
            return
        self.own_bucket.clear()
        self._drop_entries_of_models_and_owners()

    def _drop_entries_of_models_and_owners(self) -> None:
        """Drops every entry but those of keys without a model."""
        for model in list(self.models):
            self._drop_model_bucket(model)
        self.models.clear()
        for owner in list(self.owners):
            self.forget_owner(owner)
        for shared_bucket in list(self.shared_buckets.values()):
            shared_bucket.clear()
        self._drop_owner_values()
        # An owner fills its bucket without telling the cache.
        self.may_hold_entries = bool(self.own_bucket or self.owners or self.shared_buckets)

    def forget_model(self, model: type[Model]) -> None:
        """Drops every entry cached for ``model`` - every model's when the entries depend on other
        models."""
        if self.depends_on_other_models:
            self.generation += 1
            if self.may_hold_entries:
                self._drop_entries_of_models_and_owners()
            return
        if not self.may_hold_entries:
            return
        if model in self.owners:
            self.forget_owner(model)
            return
        self._drop_model_bucket(model)
        self.models.discard(model)

    def forget_all(self) -> None:
        """Drops every entry."""
        self.clear()

    @staticmethod
    def max_size_from_env() -> int:
        """The bucket size of the statement plan caches, from
        ``ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL`` or its default when unset."""
        configured_size = os.environ.get(ENV_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL)
        return DEFAULT_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL if configured_size is None else int(configured_size)
