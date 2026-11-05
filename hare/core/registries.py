from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from typing import ClassVar

from hare.core.extensions.declarations import ExtensionContribution
from hare.core.extensions.messages import EXTENSION_REGISTRATION_MESSAGE
from hare.core.extensions.registry_snapshot import RegistrySnapshot
from hare.exceptions import ConfigurationError


class Registries:
    """What dialects, fields and packages add to hare - term renderers, type mappings, lookups, path
    transforms, QuerySet methods, dialects and drivers. SQL, row-reading plans and filter
    descriptions are cached from them, so every addition goes through ``changed()``; the next
    queries rebuild what they need. Each process builds its own caches.
    """

    #: Drops every cache built from the registries - set once the app registry, which knows
    #: those caches, is loaded; before that no query has run, so nothing is cached.
    forget_all_caches: ClassVar[Callable[[], None] | None] = None
    snapshot: ClassVar[RegistrySnapshot] = RegistrySnapshot()
    execution_snapshot: ClassVar[ContextVar[RegistrySnapshot | None]] = ContextVar(
        "hare_registry_snapshot", default=None
    )
    lock: ClassVar[RLock] = RLock()
    collecting: ClassVar[ContextVar[bool]] = ContextVar("hare_extension_collecting", default=False)

    @classmethod
    def current(cls) -> RegistrySnapshot:
        """The execution's declarations, or the latest publication outside an execution.

        Publication never changes an active execution. Nested statements inherit its
        snapshot; a later independent execution starts from the latest publication.
        """
        snapshot = cls.execution_snapshot.get()
        return cls.snapshot if snapshot is None else snapshot

    @classmethod
    @contextmanager
    def bind(cls) -> Generator[RegistrySnapshot]:
        """Keep one snapshot during synchronous compilation, including renderer callbacks."""
        snapshot = cls.current()
        token = cls.execution_snapshot.set(snapshot)
        try:
            yield snapshot
        finally:
            cls.execution_snapshot.reset(token)

    @classmethod
    def register(cls, contribution: ExtensionContribution) -> None:
        """Publish all declarations together, then invalidate dependent caches exactly once."""
        if cls.collecting.get():
            raise ConfigurationError(EXTENSION_REGISTRATION_MESSAGE)
        from hare.core.extensions.registry_builder import RegistryBuilder

        with cls.lock:
            builder = RegistryBuilder(cls.snapshot)
            token = cls.collecting.set(True)
            try:
                builder.add(contribution)
                snapshot = builder.build()
            finally:
                cls.collecting.reset(token)
            if snapshot is cls.snapshot:
                return
            cls.snapshot = snapshot
            cls.changed()

    @classmethod
    def changed(cls) -> None:
        """Drops every cache built from the registries - called by each registration."""
        if cls.forget_all_caches is not None:
            cls.forget_all_caches()
