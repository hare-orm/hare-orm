from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, TypeVar

from hare.core.registries import Registries

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.core.caching.forgettable_cache import ForgettableCache

TForgettableCache = TypeVar("TForgettableCache", bound=ForgettableCache)


class Caches:
    """Every process-wide cache, and dropping them: what a cache holds for some models when they
    change or leave the registry, everything when a registry (``Registries``) changes. A cache is a
    ``Cache`` or a ``ModelCache`` (both registered when created) or another object registered with
    ``register()``; nothing else has to know it exists."""

    #: Every registered cache.
    registered: ClassVar[list[ForgettableCache]] = []

    @classmethod
    def register(cls, cache: TForgettableCache) -> TForgettableCache:
        """Registers a cache, to be dropped with the models' caches.

        Args:
            cache: The cache.

        Returns:
            The cache itself.
        """
        cls.registered.append(cache)
        return cache

    @classmethod
    def forget_all_caches(cls) -> None:
        """Drops every cache built from the registries (``Registries``) - every registered cache -
        so a lookup registered after ``Hare.init()`` is known to every model."""
        for cache in cls.registered:
            cache.forget_all()

    @classmethod
    def forget_model_caches(cls, models: Iterable[type[Model]]) -> None:
        """Drops what every registered cache holds for ``models`` - needed whenever they change
        after first use, or leave the registry.

        Args:
            models: The affected model classes.
        """
        models = list(models)
        for cache in cls.registered:
            for model in models:
                cache.forget_model(model)


Registries.forget_all_caches = Caches.forget_all_caches
