from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class ForgettableCache(Protocol):
    """What ``Caches`` needs of a registered cache."""

    def forget_model(self, model: type[Model]) -> None:
        """Drops what the cache holds for ``model``."""

    def forget_all(self) -> None:
        """Drops everything the cache holds."""
