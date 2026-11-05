from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast
from weakref import WeakSet

from hare.core.caching.cache import Cache
from hare.core.constants import CACHE_MISS

if TYPE_CHECKING:
    from hare.models import Model

P = ParamSpec("P")
TCachedValue = TypeVar("TCachedValue")
TFact = TypeVar("TFact")


class ModelCache(MutableMapping["type[Model]", TCachedValue]):
    """A cache of one value per model class, registered in ``Caches``. The value is kept on the
    model itself, so it lives as long as the class - a value referring back to its model never keeps
    a collected model alive.

    Args:
        depends_on_other_models: Whether an entry describes other models too - a change to any model
            then drops every entry.
    """

    # A cache is one object - two caches holding the same entries are still two caches (the
    # registry finds and removes a cache by it).
    __eq__ = object.__eq__
    __hash__ = object.__hash__

    def __init__(self, depends_on_other_models: bool = False) -> None:
        self.depends_on_other_models = depends_on_other_models
        #: The models holding a value of this cache - held weakly; used by iteration, ``len()``
        #: and ``clear()``.
        self.models: WeakSet[type[Model]] = WeakSet()
        self.storage: Cache[TCachedValue] = Cache(1, holds_sql=False, depends_on_other_models=depends_on_other_models)

    @staticmethod
    def fact(
        *, depends_on_other_models: bool = False
    ) -> Callable[[Callable[Concatenate[type[Model], P], TFact]], Callable[Concatenate[type[Model], P], TFact]]:
        """Makes a function of a model a fact of the model - what follows from its fields and the
        models it is related to (its backward relations, whether a CASCADE from it reaches a
        PROTECT, ...). The fact is computed the first time it is asked for and kept in a
        ``ModelCache`` of its own (the function's ``cache``) until the model changes.

        Args:
            depends_on_other_models: Whether the fact describes other models too (a walk over
                relations) - then a change to any model makes it stale.

        Returns:
            The decorator. The decorated function's first argument is the model; any further
            arguments only steer the computation and are not part of the fact's identity.
        """

        def decorator(
            compute: Callable[Concatenate[type[Model], P], TFact],
        ) -> Callable[Concatenate[type[Model], P], TFact]:
            cache: ModelCache[TFact] = ModelCache(depends_on_other_models)

            @wraps(compute)
            def get(model: type[Model], *args: P.args, **kwargs: P.kwargs) -> TFact:
                value: Any = cache.get(model, CACHE_MISS)
                if value is CACHE_MISS:
                    value = cache[model] = compute(model, *args, **kwargs)
                return value

            get.cache = cache  # type: ignore[attr-defined]
            return get

        return decorator

    def get(self, model: type[Model], default: Any = None) -> Any:
        return self.storage.get_value(model, default)

    def __getitem__(self, model: type[Model]) -> TCachedValue:
        value = self.get(model, CACHE_MISS)
        if value is CACHE_MISS:
            raise KeyError(model)
        return cast("TCachedValue", value)

    def __contains__(self, model: object) -> bool:
        return hasattr(model, "_meta") and self.get(cast("type[Model]", model), CACHE_MISS) is not CACHE_MISS

    def __setitem__(self, model: type[Model], value: TCachedValue) -> None:
        with self.storage.lock:
            self.storage.set_value(model, value)
            self.models.add(model)

    def __delitem__(self, model: type[Model]) -> None:
        with self.storage.lock:
            del self.storage.get_bucket(model)[None]
            self.models.discard(model)

    def __iter__(self) -> Iterator[type[Model]]:
        with self.storage.lock:
            models = tuple(self.models)
        for model in models:
            if model in self:
                yield model

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def clear(self) -> None:
        with self.storage.lock:
            self.storage.clear()
            self.models.clear()

    def forget_model(self, model: type[Model]) -> None:
        with self.storage.lock:
            self.storage.forget_model(model)
            if self.depends_on_other_models:
                self.models.clear()
            else:
                self.models.discard(model)

    def forget_all(self) -> None:
        self.clear()
