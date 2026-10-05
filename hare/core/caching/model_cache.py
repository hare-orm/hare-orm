from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from functools import wraps
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast
from weakref import WeakSet

from hare.core.caching.caches import Caches
from hare.core.constants import CACHE_MISS

if TYPE_CHECKING:  # pragma: nocoverage
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
        Caches.register(self)

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
            cache_key = id(cache)
            models = cache.models

            @wraps(compute)
            def get(model: type[Model], *args: P.args, **kwargs: P.kwargs) -> TFact:
                values = model._meta.model_cache_values
                value: Any = values.get(cache_key, CACHE_MISS)
                if value is CACHE_MISS:
                    value = values[cache_key] = compute(model, *args, **kwargs)
                    models.add(model)
                return value

            get.cache = cache  # type: ignore[attr-defined]
            return get

        return decorator

    def get(self, model: type[Model], default: Any = None) -> Any:
        """The value of ``model``, or ``default`` when it has none - one lookup.

        Args:
            model: The model.
            default: Returned on a miss.

        Returns:
            The value or ``default``.
        """
        return model._meta.model_cache_values.get(id(self), default)

    def __getitem__(self, model: type[Model]) -> TCachedValue:
        values = model._meta.model_cache_values
        key = id(self)
        if key not in values:
            raise KeyError(model)
        return cast("TCachedValue", values[key])

    def __contains__(self, model: object) -> bool:
        meta = getattr(model, "_meta", None)
        return meta is not None and id(self) in meta.model_cache_values

    def __setitem__(self, model: type[Model], value: TCachedValue) -> None:
        model._meta.model_cache_values[id(self)] = value
        self.models.add(model)

    def __delitem__(self, model: type[Model]) -> None:
        del model._meta.model_cache_values[id(self)]
        self.models.discard(model)

    def __iter__(self) -> Iterator[type[Model]]:
        for model in list(self.models):
            if id(self) in model._meta.model_cache_values:
                yield model

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def clear(self) -> None:
        for model in list(self.models):
            model._meta.model_cache_values.pop(id(self), None)
        self.models.clear()

    def forget_model(self, model: type[Model]) -> None:
        """Drops the entry of ``model`` - every entry when they depend on other models.

        Args:
            model: The model that changed.
        """
        if self.depends_on_other_models:
            if self.models:
                self.clear()
        elif model._meta.model_cache_values.pop(id(self), CACHE_MISS) is not CACHE_MISS:
            self.models.discard(model)

    def forget_all(self) -> None:
        """Drops every entry."""
        self.clear()
