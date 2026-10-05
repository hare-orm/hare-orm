from __future__ import annotations

import time
from contextvars import ContextVar
from typing import ClassVar


class WrittenConnections:
    """The connections the current asyncio task wrote through, each with when it last did - the
    reads of a model written there go to it, not to a replica that may not have the write yet
    (``ConnectionRouter.db_for_read()``). Made on the task's first write; a task started from it
    sees the same writes, a request served in another task none of them."""

    #: Whether a context of the process routes models (``ConnectionRouter``) - a write checks it
    #: before ``record()``, so without routers it costs no call.
    is_recording: ClassVar[bool] = False

    #: The writes of the current task, None before its first one.
    current: ClassVar[ContextVar[WrittenConnections | None]] = ContextVar("hare_written_connections", default=None)

    def __init__(self) -> None:
        #: ``time.monotonic()`` of the last write, by connection name.
        self.written_at_by_connection: dict[str, float] = {}

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
        if written is None:
            written = WrittenConnections()
            cls.current.set(written)
        written.written_at_by_connection[connection_alias] = time.monotonic()

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
        return window_seconds is None or time.monotonic() - written_at <= window_seconds
