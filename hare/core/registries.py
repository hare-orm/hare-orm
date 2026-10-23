from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar


class Registries:
    """What dialects, fields and packages add to hare - term renderers, type mappings, lookups, path
    transforms, QuerySet methods, dialects and drivers. SQL, row-reading plans and filter
    descriptions are cached from them, so every addition goes through ``changed()``; the next
    queries rebuild what they need. Each process builds its own caches.
    """

    #: Drops every cache built from the registries - set once the app registry, which knows
    #: those caches, is loaded; before that no query has run, so nothing is cached.
    forget_all_caches: ClassVar[Callable[[], None] | None] = None

    @classmethod
    def changed(cls) -> None:
        """Drops every cache built from the registries - called by each registration."""
        if cls.forget_all_caches is not None:
            cls.forget_all_caches()
