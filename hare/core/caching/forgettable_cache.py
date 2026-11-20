from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding


class ForgettableCache(Protocol):
    """What ``Caches`` needs of a registered cache."""

    def forget_model(self, binding: ModelBinding) -> None:
        """Drops what the cache holds for ``binding``."""

    def forget_all(self) -> None:
        """Drops everything the cache holds."""
