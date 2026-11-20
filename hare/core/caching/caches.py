from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, TypeVar

from hare.core.caching.forgettable_cache import ForgettableCache

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding

TForgettableCache = TypeVar("TForgettableCache", bound=ForgettableCache)


class Caches:
    """Every process-wide cache, and dropping them: what a cache holds for some models when they
    change or leave the registry, everything when a registry (``Registries``) changes. Each ``Cache``,
    including a ``ModelCache``'s storage, registers when created. Other caches can call ``register()``."""

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
    def forget_model_caches(cls, bindings: Iterable[ModelBinding]) -> None:
        """Drops what every registered cache holds for ``models`` - needed whenever they change
        after first use, or leave the registry.

        Args:
            bindings: The bindings whose cached data is discarded.
        """
        bindings = tuple(bindings)
        for cache in cls.registered:
            for binding in bindings:
                cache.forget_model(binding)
