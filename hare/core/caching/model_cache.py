from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar
from weakref import WeakSet

from hare.core.caching.cache import Cache
from hare.core.constants import CACHE_MISS

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding

P = ParamSpec("P")
TCachedValue = TypeVar("TCachedValue")
TFact = TypeVar("TFact")


class ModelCache(MutableMapping["ModelBinding", TCachedValue]):
    """One cached fact per structural binding, with storage registered in ``Caches``. Entries live in the
    binding's cache buckets; weak enumeration never extends a graph's lifetime. A newly published
    graph starts with independent buckets.

    Args:
        depends_on_other_models: Whether an entry describes other models too - a change to any binding
            then drops every entry.
    """

    # A cache is one object - two caches holding the same entries are still two caches (the
    # registry finds and removes a cache by it).
    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def __init__(self, depends_on_other_models: bool = False) -> None:
        #: The models holding a value of this cache - held weakly; used by iteration, ``len()``
        #: and ``clear()``.
        self.bindings: WeakSet[ModelBinding] = WeakSet()
        self.storage: Cache[TCachedValue] = Cache(1, depends_on_other_models=depends_on_other_models)

    @staticmethod
    def fact(
        *, depends_on_other_models: bool = False
    ) -> Callable[[Callable[Concatenate[ModelBinding, P], TFact]], Callable[Concatenate[ModelBinding, P], TFact]]:
        """Makes a function of a binding a fact of the binding - what follows from its fields and the
        models it is related to (its backward relations, whether a CASCADE from it reaches a
        PROTECT, ...). The fact is computed the first time it is asked for and kept in a
        ``ModelCache`` of its own (the function's ``cache``) until the binding changes.

        Args:
            depends_on_other_models: Whether the fact describes other models too (a walk over
                relations) - then a change to any binding makes it stale.

        Returns:
            The decorator. The decorated function's first argument is the binding; any further
            arguments only steer the computation and are not part of the fact's identity.
        """

        def decorator(
            compute: Callable[Concatenate[ModelBinding, P], TFact],
        ) -> Callable[Concatenate[ModelBinding, P], TFact]:
            cache: ModelCache[TFact] = ModelCache(depends_on_other_models)
            read_value = cache.storage.get_model_value

            @wraps(compute)
            def get(binding: ModelBinding, *args: P.args, **kwargs: P.kwargs) -> TFact:
                value: Any = read_value(binding, CACHE_MISS)
                if value is CACHE_MISS:
                    value = cache[binding] = compute(binding, *args, **kwargs)
                return value

            get.cache = cache  # type: ignore[attr-defined]
            return get

        return decorator

    def get(self, binding: ModelBinding, default: Any = None) -> Any:
        return self.storage.get_model_value(binding, default)

    def __getitem__(self, binding: ModelBinding) -> TCachedValue:
        value = self.get(binding, CACHE_MISS)
        if value is CACHE_MISS:
            raise KeyError(binding)
        return value

    def __contains__(self, binding: object) -> bool:
        return hasattr(binding, "cache_buckets") and self.get(binding, CACHE_MISS) is not CACHE_MISS  # type: ignore[arg-type]

    def __setitem__(self, binding: ModelBinding, value: TCachedValue) -> None:
        with self.storage.lock:
            self.storage.set_value(binding, value)
            self.bindings.add(binding)

    def __delitem__(self, binding: ModelBinding) -> None:
        with self.storage.lock:
            del self.storage.get_bucket(binding)[None]
            self.bindings.discard(binding)

    def __iter__(self) -> Iterator[ModelBinding]:
        with self.storage.lock:
            bindings = tuple(self.bindings)
        for binding in bindings:
            if binding in self:
                yield binding

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def clear(self) -> None:
        with self.storage.lock:
            self.storage.clear()
            self.bindings.clear()
