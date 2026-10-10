from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.client.connection_wrapper import ConnectionWrapper
from hare.dialects.base.client.transaction_lifecycle.pending_statements import PendingStatements
from hare.dialects.base.transactions.savepoints.savepoint_span import current_savepoint_span
from rust.native import pg

if TYPE_CHECKING:
    from types import TracebackType

    from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_transaction_client import RustPgTransactionClient


class RustPgTransactionConnectionWrapper(ConnectionWrapper[pg.Transaction]):
    """The connection of a Rust driver transaction - its ``pg.Transaction``, held under a span of the
    transaction's savepoint lock for the block, the SAVEPOINTs still pending sent ahead of it."""

    __slots__ = ()

    def __init__(self, client: RustPgTransactionClient) -> None:
        self.client = client
        self._span = None

    async def __aenter__(self) -> pg.Transaction:
        client: RustPgTransactionClient = self.client  # type: ignore[assignment]  # cast() is a call per block
        savepoint_lock = client._savepoint_lock
        current_span = current_savepoint_span.get()
        span = savepoint_lock.open_without_waiting(current_span)
        if span is None:
            span = await savepoint_lock.wait_for_query_span(current_span)
        try:
            # The transaction may have started ending (or ended) while the block waited.
            client._check_statement_allowed()
            if (client._outer_transaction or client)._pending_savepoints:
                await PendingStatements.send_pending_savepoints(client)
            self.connection = client._active_native_transaction
        except BaseException:
            savepoint_lock.release(span)
            raise
        self._span = span
        return self.connection

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        span, self._span = self._span, None
        if span is not None:
            self.client._savepoint_lock.release(span)  # type: ignore[attr-defined]
