from __future__ import annotations

import time
from collections.abc import Mapping
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


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
    def record(cls, connection: DatabaseClient) -> None:
        """Record a write for the application's handler that supplied its actual client.

        Explicit clients may come from another application than the active context.
        A standalone client without a handler has no application's read history.

        Args:
            connection: The client the write ran through.
        """
        handler = connection.connection_handler
        if handler is None or handler.routing_router is None or not handler.routing_router.has_routers:
            return
        identity = handler.routing_identity
        written = cls.current.get()
        writes = {} if written is None else written.writes
        application_writes = MappingProxyType(
            {**writes.get(identity, {}), connection.connection_alias: cls.get_time()}
        )
        cls.current.set(cls(MappingProxyType({**writes, identity: application_writes})))

    @classmethod
    def publish_from_context(cls, context: Context, previous: WrittenConnections | None) -> None:
        """Publish new writes from a completed ORM operation's internal task.

        Ordinary user tasks retain independent history. Only entries changed by the
        operation are merged; inherited entries cannot restore forgotten history.
        Other ContextVars and published immutable snapshots are untouched.
        """
        completed = context.get(cls.current)
        current = cls.current.get()
        if completed is None or completed is previous or completed is current:
            return
        writes = {} if current is None else dict(current.writes)
        changed = False
        for identity, times in completed.writes.items():
            inherited = {} if previous is None else previous.writes.get(identity, {})
            additions = {alias: timestamp for alias, timestamp in times.items() if inherited.get(alias) != timestamp}
            if additions:
                existing = writes.get(identity, {})
                writes[identity] = MappingProxyType(
                    {
                        **existing,
                        **{
                            alias: max(existing.get(alias, timestamp), timestamp)
                            for alias, timestamp in additions.items()
                        },
                    }
                )
                changed = True
        if changed:
            cls.current.set(cls(MappingProxyType(writes)))

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
