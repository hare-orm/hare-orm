from __future__ import annotations

import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class WrittenConnections:
    """The connections the current asyncio task wrote through, each with when it last did - the
    reads of a model written there go to it, not to a replica that may not have the write yet
    (``ConnectionRouter.db_for_read()``). Made on the task's first write; a task started from it
    sees the writes made before it, a request served in another task none of them - a task records
    its own writes in a copy of the ones it started with, never in its parent's."""

    #: Whether a context of the process routes models (``ConnectionRouter``) - a write checks it
    #: before ``record()``, so without routers it costs no call.
    is_recording: ClassVar[bool] = False

    #: The writes of the current task, None before its first one.
    current: ClassVar[ContextVar[WrittenConnections | None]] = ContextVar("hare_written_connections", default=None)
    empty_writes: ClassVar[Mapping[str, float]] = MappingProxyType({})

    writes: Mapping[object, Mapping[str, float]] = field(default_factory=lambda: MappingProxyType({}))

    @staticmethod
    def get_time() -> float:
        """Monotonic time used for the read-after-write window."""
        return time.monotonic()

    @property
    def written_at_by_connection(self) -> Mapping[str, float]:
        """Monotonic write times for the active application only."""
        from hare.core.hare_context import HareContext

        context = HareContext.get_current()
        return self.writes.get(None if context is None else context.identity, WrittenConnections.empty_writes)

    @classmethod
    def record(cls, connection_alias: str) -> None:
        """Records a write through a connection in the current task - when the task's context
        routes models: a write made before its routers existed changes no read.

        Args:
            connection_alias: The connection.
        """
        # Local import: the context imports the routing package.
        from hare.core.hare_context import HareContext

        context = HareContext.get_current()
        if context is None or not context.router.has_routers:
            return
        written = cls.current.get()
        writes = {} if written is None else written.writes
        application_writes = MappingProxyType({**writes.get(context.identity, {}), connection_alias: cls.get_time()})
        cls.current.set(cls(MappingProxyType({**writes, context.identity: application_writes})))

    @classmethod
    def forget(cls) -> None:
        """Forget this application's write history in the current task, retaining other applications."""
        from hare.core.hare_context import HareContext

        written = cls.current.get()
        context = HareContext.get_current()
        if written is None or context is None or context.identity not in written.writes:
            return
        writes = {identity: values for identity, values in written.writes.items() if identity is not context.identity}
        cls.current.set(cls(MappingProxyType(writes)) if writes else None)

    def reads_from(self, connection_alias: str, window_seconds: float | None) -> bool:
        """Whether a read goes to a connection the task wrote through.

        Args:
            connection_alias: The connection a model is written through.
            window_seconds: How long after the last write the reads stay there, None for the rest
                of the task.

        Returns:
            True while the reads stay on the connection.
        """
        written_at = self.written_at_by_connection.get(connection_alias)
        if written_at is None:
            return False
        return window_seconds is None or self.get_time() - written_at <= window_seconds
