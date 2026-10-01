import abc
import asyncio
import contextlib
import contextvars
import inspect
import time
import weakref
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, ClassVar, cast

from hare.core.connections import Connections
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.constants import (
    SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS,
    TRANSACTION_END_WAIT_TIMEOUT_SECONDS,
)
from hare.dialects.base.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.base.nested_transaction_context import NestedTransactionContext
from hare.dialects.base.savepoint_span import SavepointSpan, current_savepoint_span
from hare.dialects.base.transaction_callback import TransactionCallback
from hare.dialects.base.transaction_context import TransactionContext
from hare.exceptions import (
    QueryError,
    TransactionManagementError,
    UnSupportedError,
)
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_call import QueryCall
from hare.instrumentation.query_tags import QueryTags
from hare.transactions.enums import TransactionEventType
from hare.transactions.options import TransactionOptions


class TransactionClient(DatabaseClient, abc.ABC):
    """An interface of the DB client that supports transactions."""

    is_transaction_client: ClassVar[bool] = True
    _finalized: bool = False
    #: True between a successful PREPARE TRANSACTION and the release of the connection to the pool -
    #: `_finalized` alone doesn't tell that the release is still due.
    _prepared_without_release: bool = False
    #: Set when a shielded COMMIT/ROLLBACK or savepoint statement hit its wait bound with its task
    #: still running - the connection may still be in use and isn't given back to the pool.
    _shielded_operation_abandoned: bool = False
    #: The abandoned task of a savepoint's RELEASE/ROLLBACK TO - the nested context releases its
    #: span only once it finishes. Reset to None by the context that picks it up.
    _shielded_savepoint_abandoned_task: "asyncio.Future[Any] | None" = None
    #: The client this transaction was opened on - the outer transaction's client for a savepoint.
    _parent: DatabaseClient
    #: Set on the top-level wrapper once any statement of the transaction (savepoints included)
    #: failed or was interrupted - on Postgres that may have aborted the transaction, so its
    #: COMMIT first checks whether it is still usable.
    _statement_failed: bool = False
    #: Set on the top-level wrapper once a statement run outside every savepoint (or in a savepoint
    #: later released) was cancelled while in flight - whether it landed is unknown, so the
    #: transaction can't commit.
    _statement_interrupted: bool = False
    #: Set on the top-level wrapper once its real COMMIT/ROLLBACK has started - from then on no
    #: other statement or savepoint may start on the transaction.
    _ending: bool = False
    #: The span the running top-level COMMIT/ROLLBACK holds - statements it issues itself run
    #: under it and stay allowed while ``_ending`` is set. None while no COMMIT/ROLLBACK runs.
    _ending_span: "SavepointSpan | None" = None
    #: Serializes this transaction's statements and savepoints across tasks - one instance per
    #: top-level transaction, shared by every nested wrapper (created by each backend's wrapper).
    _savepoint_lock: "NestedSavepointLock"
    #: The span of the savepoint this nested wrapper opened - set by NestedTransactionContext, None
    #: on a top-level wrapper.
    _savepoint_span: "SavepointSpan | None" = None
    #: Set on a nested wrapper from savepoint() until a statement run under it sends the SAVEPOINT -
    #: a nested block running no statement sends nothing.
    _savepoint_pending: bool = False
    #: The nested wrappers of the transaction whose SAVEPOINT is still to be sent, outermost first -
    #: kept on the top-level wrapper.
    _pending_savepoints: "list[TransactionClient]"
    #: Set on the top-level wrapper of a driver that sends its BEGIN with the transaction's first
    #: statement (``_driver_send_begin()``), from begin() until a statement sends it - a block
    #: running no statement sends nothing.
    _begin_pending: bool = False

    def __init__(self, connection: DatabaseClient, savepoint_lock: NestedSavepointLock | None = None) -> None:
        """
        Args:
            connection: The client the transaction is opened on - the outer transaction's client
                for a savepoint.
            savepoint_lock: The lock of the transaction a savepoint is nested in - a top-level
                transaction makes its own, shared by every savepoint nested in it.
        """
        self._parent = connection
        #: The outermost transaction wrapper this one is nested in - None at the top level: a
        #: reference to itself would keep the wrapper, and the connection it holds, alive until a
        #: garbage collection.
        self._outer_transaction: TransactionClient | None = (
            connection._get_top_level_transaction() if isinstance(connection, TransactionClient) else None
        )
        self.log = connection.log
        self.connection_name = connection.connection_name
        self.fetch_inserted = connection.fetch_inserted
        self.features = connection.features
        self._savepoint_lock = savepoint_lock if savepoint_lock is not None else NestedSavepointLock()
        #: This wrapper's own savepoint name - set only on a nested wrapper, by savepoint().
        self._savepoint_name: str | None = None
        self._finalized = False
        self._on_commit_callbacks = []
        self._on_rollback_callbacks = []
        self._pending_savepoints = []

    def _in_transaction(self, options: TransactionOptions = TransactionOptions.DEFAULT) -> TransactionContext:
        self._check_nested_transaction_options(options)
        client = type(self)(self, self._savepoint_lock)
        client._set_transaction_options(self._transaction_options)
        return NestedTransactionContext(client, self._savepoint_lock)

    async def close(self) -> None:
        # Closing the connection of an open transaction isn't coherent on any backend - and on a
        # pooled one would close the pool every other client of the alias uses.
        raise TransactionManagementError("Cannot close a connection while inside a transaction")

    async def _take_transaction_resources(self) -> None:
        """Takes what a top-level transaction holds for its whole life, before BEGIN - nothing by
        default. Gives back whatever it took when it raises."""

    async def _undo_failed_begin(self) -> None:
        """Ends a transaction whose BEGIN raised but may have landed - nothing by default."""

    async def _give_back_transaction_resources(self) -> None:
        """Gives back what ``_take_transaction_resources()`` took - nothing by default."""

    async def _end_unfinished_transaction(self) -> None:
        """Ends what a top-level transaction left open on the driver once its block exits -
        nothing by default."""

    def get_non_transactional_client(self) -> DatabaseClient:
        """The shared, non-transactional client this transaction (and every transaction it is
        nested in) was opened on.

        Returns:
            The alias's root client.
        """
        client: DatabaseClient = self
        while isinstance(client, TransactionClient):
            client = client._parent
        return client

    def _get_top_level_transaction(self) -> "TransactionClient":
        """The outermost transaction wrapper this one (a savepoint, or itself) belongs to.

        Returns:
            The top-level transaction wrapper.
        """
        outer_transaction = self._outer_transaction
        return self if outer_transaction is None else outer_transaction

    def _mark_statement_failed(self) -> None:
        """Records on the top-level wrapper that a statement of this transaction failed."""
        self._get_top_level_transaction()._statement_failed = True

    def _mark_statement_interrupted(self) -> None:
        """Records that a statement was cancelled while in flight, on the innermost savepoint it
        ran under or, outside every savepoint, on the transaction itself."""
        self._mark_interrupted_level(current_savepoint_span.get())
        self._mark_statement_failed()

    def _mark_interrupted_level(self, span: SavepointSpan | None) -> None:
        """Marks the savepoint of ``span`` (the nearest one of this transaction), or the whole
        transaction when there is none, as holding an interrupted statement.

        Args:
            span: A savepoint span, or None for the transaction itself.
        """
        top_level_transaction = self._get_top_level_transaction()
        own_span = top_level_transaction._savepoint_lock.get_own_ancestor_span(span)
        if own_span is None:
            top_level_transaction._statement_interrupted = True
        else:
            own_span.statement_interrupted = True

    def _is_transaction_finished(self) -> bool:
        """Whether this wrapper's own savepoint or the whole transaction has already ended."""
        return self._finalized or self._get_top_level_transaction()._finalized

    def _is_inside_ending_operation(self) -> bool:
        """Whether the calling context runs inside this transaction's own COMMIT/ROLLBACK."""
        ending_span = self._get_top_level_transaction()._ending_span
        current_span = current_savepoint_span.get()
        return ending_span is not None and current_span is not None and current_span.is_within(ending_span)

    def _check_statement_allowed(self) -> None:
        """Refuses a new statement on a transaction that ended or is ending.

        Raises:
            TransactionManagementError: if this wrapper's savepoint or the whole transaction has
                ended, or the transaction's COMMIT/ROLLBACK has already started.
        """
        # _is_transaction_finished() and _get_top_level_transaction(), without their calls - this
        # runs on every statement of a transaction.
        outer_transaction = self._outer_transaction
        top_level_transaction = self if outer_transaction is None else outer_transaction
        if self._finalized or top_level_transaction._finalized:
            raise TransactionManagementError("Cannot execute query: transaction already finalised")
        if top_level_transaction._ending and not self._is_inside_ending_operation():
            raise TransactionManagementError(
                "Cannot execute query: the transaction is already being committed or rolled back"
            )

    def _get_registration_span(self) -> "SavepointSpan | None":
        """The innermost savepoint of this transaction open in the calling context, None at the
        transaction's own level."""
        return self._savepoint_lock.get_own_ancestor_span(current_savepoint_span.get())

    def _add_on_commit_callback(self, callback: Callable[[], Any]) -> None:
        """Registers an on_commit() callback at the savepoint level of the calling context.

        Args:
            callback: run once the whole transaction commits.
        """
        callback_entry = TransactionCallback(callback, self._get_registration_span())
        self._get_top_level_transaction()._on_commit_callbacks.append(callback_entry)

    def _add_on_rollback_callback(self, callback: Callable[[], Any]) -> None:
        """Registers an on_rollback() callback at the savepoint level of the calling context.

        Args:
            callback: run once the transaction, or the savepoint it is registered in, rolls back.
        """
        callback_entry = TransactionCallback(callback, self._get_registration_span())
        self._get_top_level_transaction()._on_rollback_callbacks.append(callback_entry)

    def _mark_savepoint_operation_abandoned(self, task: "asyncio.Future[Any]") -> None:
        """``on_timeout`` callback for a savepoint-level (RELEASE/ROLLBACK TO) shielded call -
        see ``_shielded_operation_abandoned``/``_shielded_savepoint_abandoned_task``'s own
        docstrings for what each half is read by."""
        self._shielded_operation_abandoned = True
        self._shielded_savepoint_abandoned_task = task

    #: time.monotonic() at the top-level BEGIN - the transaction's duration is computed from it.
    #: None on a savepoint's client.
    _transaction_started_at: float | None = None
    #: on_commit() callbacks of the whole transaction, kept on the top-level wrapper. Each backend
    #: creates a real list in its own transaction client's __init__ - this is just the type
    #: annotation (a mutable class-level default would be shared across all instances).
    _on_commit_callbacks: list[TransactionCallback]
    #: on_rollback() callbacks of the whole transaction, kept on the top-level wrapper - fired by a
    #: real top-level rollback, or by the rollback of the savepoint they were registered in.
    _on_rollback_callbacks: list[TransactionCallback]
    #: Set by the owning top-level transaction context. Weak, so a client abandoned without its
    #: context exiting is still freed by plain reference counting - which is what returns a
    #: pinned connection to its pool.
    _transaction_context_ref: "weakref.ref[TransactionContext] | None" = None
    #: The error a failed ``_release_transaction_resources()`` raised, kept for the owning
    #: context's __aexit__ to report - it knows whether the block's own exception is already in
    #: flight, which a failed release must never replace.
    _release_failure: Exception | None = None
    #: How this transaction runs - set on the top-level wrapper by ``_in_transaction(options)``
    #: and inherited by every nested one.
    _transaction_options: TransactionOptions = TransactionOptions.DEFAULT

    def _set_transaction_options(self, options: TransactionOptions) -> None:
        """Records the options this wrapper's transaction runs with.

        Args:
            options: The transaction's options.
        """
        self._transaction_options = options

    def _check_nested_transaction_options(self, options: TransactionOptions) -> None:
        """Rejects options a nested transaction (a savepoint) can't honour - it runs inside the
        transaction it is nested in, with that transaction's options.

        Args:
            options: The options the nested transaction asks for.

        Raises:
            QueryError: A read-only transaction is nested inside a read-write one, a
                nested transaction asks for its own statement timeout, or for another isolation
                level than the one of the transaction it is nested in.
        """
        if options.read_only and not self._transaction_options.read_only:
            raise QueryError(
                "A read-only transaction can't be nested inside a read-write one - open it outside "
                "any enclosing transaction."
            )
        if options.statement_timeout is not None:
            raise QueryError(
                "statement_timeout can only be set on the outermost transaction - a nested "
                "transaction runs with the timeout of the transaction it is nested in."
            )
        if options.isolation is not None and options.isolation != self._transaction_options.isolation:
            raise QueryError(
                f"A nested transaction can't run at the {options.isolation!s} isolation level - it runs at "
                "the isolation level of the transaction it is nested in; set isolation on the outermost one."
            )

    async def _apply_transaction_restrictions(self) -> None:
        """Makes the just-begun top-level transaction run at its isolation level, read-only and/or
        time-limited, through the ordinary query path so every statement is observed."""
        for statement in self._get_transaction_restriction_statements(self._transaction_options):
            await self.execute(statement)

    #: This wrapper's own savepoint - set only on a nested wrapper, by savepoint().
    _savepoint_name: str | None = None
    #: The real COMMIT/ROLLBACK about to be sent, kept until its callbacks have run - a later call
    #: that finds the transaction already ended by an interrupted earlier one fires the callbacks
    #: of that operation.
    _pending_top_level_operation: TransactionEventType | None = None

    @abc.abstractmethod
    async def _driver_begin(self) -> None:
        """Starts the transaction on the connection."""

    async def _driver_send_begin(self) -> None:
        """Sends the BEGIN ``_driver_begin()`` left pending (``_begin_pending``) - of a driver that
        begins with the transaction's first statement."""
        raise NotImplementedError  # pragma: nocoverage

    @abc.abstractmethod
    async def _driver_commit(self) -> None:
        """Sends the transaction's COMMIT."""

    @abc.abstractmethod
    async def _driver_rollback(self) -> None:
        """Sends the transaction's ROLLBACK."""

    @abc.abstractmethod
    async def _driver_savepoint(self, name: str) -> None:
        """Opens a savepoint.

        Args:
            name: The savepoint's name.
        """

    @abc.abstractmethod
    async def _driver_release_savepoint(self, name: str) -> None:
        """Releases a savepoint.

        Args:
            name: The savepoint's name.
        """

    @abc.abstractmethod
    async def _driver_rollback_to_savepoint(self, name: str) -> None:
        """Rolls back to a savepoint.

        Args:
            name: The savepoint's name.
        """

    @abc.abstractmethod
    def _get_new_savepoint_name(self) -> str:
        """A savepoint name not used on this connection yet."""

    def _has_begun(self) -> bool:
        """Whether this top-level wrapper's transaction was started."""
        return True

    def _is_connection_lost(self, error: BaseException) -> bool:
        """Whether a driver error of a COMMIT/ROLLBACK means the connection is gone - the server
        rolls such a transaction back.

        Args:
            error: The error the driver raised.
        """
        return False

    def _is_commit_outcome_unknown(self, error: BaseException) -> bool:
        """Whether a lost connection leaves it unknown if a COMMIT in flight landed.

        Args:
            error: The connection error.
        """
        return False

    def _is_commit_rejection(self, error: BaseException) -> bool:
        """Whether the database answered the COMMIT with an error - the transaction is over.

        Args:
            error: The error the driver raised.
        """
        return False

    def _is_transaction_finished_error(self, error: BaseException) -> bool:
        """Whether the driver reports that an earlier, interrupted COMMIT/ROLLBACK already ended
        the transaction.

        Args:
            error: The error the driver raised.
        """
        return False

    async def _check_commit_allowed(self) -> None:
        """Refuses a commit() the transaction's state rules out, before anything is sent."""

    def _check_savepoint_allowed(self) -> None:
        """Refuses a savepoint or its release the transaction's state rules out."""

    async def _before_top_level_end(self, event: TransactionEventType) -> Exception | None:
        """Runs right before the real COMMIT/ROLLBACK, holding the transaction.

        Args:
            event: ``COMMIT`` or ``ROLLBACK``.

        Returns:
            An error to raise once the operation has landed and its callbacks ran, None for none.
        """
        return None

    async def _after_rejected_commit(self, commit_error: BaseException) -> None:
        """Leaves the connection out of the transaction whose COMMIT the database rejected.

        Args:
            commit_error: The error the COMMIT raised.
        """

    def _mark_finalized(self) -> None:
        self._finalized = True

    def _mark_top_level_operation_abandoned(self, task: "asyncio.Future[Any]") -> None:
        self._shielded_operation_abandoned = True

    @staticmethod
    def _do_nothing() -> None:
        """``on_landed`` of a shielded call that has nothing to record."""

    async def begin(self) -> None:
        # Shielded: the BEGIN can land after the awaiting task was cancelled, and the connection
        # would then go back to the pool mid-transaction.
        await self._run_shielded_from_cancellation(self._driver_begin(), on_landed=self._do_nothing)

    async def savepoint(self) -> None:
        self._check_savepoint_allowed()
        self._savepoint_name = self._get_new_savepoint_name()
        # The SAVEPOINT goes out ahead of the first statement run under it
        # (_send_pending_savepoints()).
        self._savepoint_pending = True
        self._get_top_level_transaction()._pending_savepoints.append(self)

    async def _send_pending_statements(self) -> None:
        """Sends the transaction's BEGIN and the SAVEPOINTs no statement has sent yet - called with
        the transaction's connection lock held, right before a statement."""
        top_level_transaction = self._outer_transaction or self
        if top_level_transaction._begin_pending:
            # Shielded for the reason _send_pending_begin() gives.
            await self._run_shielded_from_cancellation(
                top_level_transaction._driver_send_begin(), on_landed=top_level_transaction._mark_begin_sent
            )
        if top_level_transaction._pending_savepoints:
            await self._send_pending_savepoints()

    async def _send_pending_begin(self) -> None:
        """Sends the transaction's BEGIN when no statement has sent it yet - called with the
        transaction's connection lock held, right before a statement. Shielded: a BEGIN landing
        after the awaiting task was cancelled must be known, or the transaction would never end.
        """
        top_level_transaction = self._get_top_level_transaction()
        if top_level_transaction._begin_pending:
            await self._run_shielded_from_cancellation(
                top_level_transaction._driver_send_begin(), on_landed=top_level_transaction._mark_begin_sent
            )

    def _mark_begin_sent(self) -> None:
        self._begin_pending = False

    async def _send_pending_savepoints(self) -> None:
        """Sends the SAVEPOINT of every savepoint of the transaction no statement has sent yet,
        outermost first - right before a statement that holds its turn on the savepoint lock, so
        every open savepoint encloses it. Each is shielded: a SAVEPOINT landing after the awaiting
        task was cancelled would leave one more savepoint on the connection than the savepoint lock
        tracks.
        """
        current_span = current_savepoint_span.get()
        if current_span is None:
            return
        # Only the savepoints the calling context runs in - a statement without a span of its own
        # (a stream's fetch) may run beside a sibling's savepoint.
        for nested_client in list(self._get_top_level_transaction()._pending_savepoints):
            savepoint_span = nested_client._savepoint_span
            if (
                nested_client._savepoint_pending
                and savepoint_span is not None
                and current_span.is_within(savepoint_span)
            ):
                await self._run_shielded_from_cancellation(
                    nested_client._driver_savepoint(cast("str", nested_client._savepoint_name)),
                    on_landed=nested_client._drop_pending_savepoint,
                )

    def _drop_pending_savepoint(self) -> None:
        """Takes this wrapper's savepoint off the pending ones - sent, or ended before any statement
        ran under it. Once."""
        if self._savepoint_pending:
            self._savepoint_pending = False
            self._get_top_level_transaction()._pending_savepoints.remove(self)

    async def release_savepoint(self) -> None:
        savepoint_name = self._savepoint_name
        if savepoint_name is None:
            raise TransactionManagementError("Transaction is in invalid state")
        if self._is_transaction_finished():
            raise TransactionManagementError("Transaction already finalised")
        self._check_savepoint_allowed()
        if self._savepoint_pending:
            # No statement ran under it - nothing was sent, nothing is released.
            self._drop_pending_savepoint()
            self._mark_finalized()
            return
        # Not a real commit - the on_commit() callbacks only run once the outermost level commits.
        await self._run_shielded_from_cancellation(
            self._driver_release_savepoint(savepoint_name),
            on_landed=self._mark_finalized,
            on_timeout=self._mark_savepoint_operation_abandoned,
        )

    async def savepoint_rollback(self) -> None:
        savepoint_name = self._savepoint_name
        if savepoint_name is None:
            raise TransactionManagementError("Transaction is in invalid state")
        if self._is_transaction_finished():
            raise TransactionManagementError("Transaction already finalised")

        async def roll_back_and_fire_callbacks() -> None:
            # One shielded coroutine: a cancellation between the ROLLBACK TO and the callbacks
            # would land the one and skip the other. A savepoint no statement ran under has
            # nothing to roll back - nothing was sent.
            if self._savepoint_pending:
                self._drop_pending_savepoint()
            else:
                await self._driver_rollback_to_savepoint(savepoint_name)
            self._finalized = True
            await self._discard_rolled_back_savepoint_callbacks()

        await self._run_shielded_from_cancellation(
            roll_back_and_fire_callbacks(),
            on_landed=self._do_nothing,
            on_timeout=self._mark_savepoint_operation_abandoned,
        )

    async def commit(self) -> None:
        await self._check_commit_allowed()
        # A nested wrapper shares the transaction - its commit releases its savepoint.
        if self._savepoint_name is not None:
            await self.release_savepoint()
            return
        await self._end_top_level(TransactionEventType.COMMIT)

    async def rollback(self) -> None:
        if self._savepoint_name is not None:
            await self.savepoint_rollback()
            return
        await self._end_top_level(TransactionEventType.ROLLBACK)

    async def _end_top_level(self, event: TransactionEventType) -> None:
        """Sends the real COMMIT/ROLLBACK of this top-level transaction and completes it: hands
        its resources back, runs the matching callbacks and records the event.

        Args:
            event: ``COMMIT`` or ``ROLLBACK``.

        Raises:
            TransactionManagementError: The transaction never began, has ended, or can't commit.
        """
        if not self._has_begun():
            raise TransactionManagementError("Transaction is in invalid state")
        if self._finalized:
            raise TransactionManagementError("Transaction already finalised")
        is_commit = event is TransactionEventType.COMMIT
        previous_pending = self._pending_top_level_operation
        self._pending_top_level_operation = event
        landed_by_this_call = False

        async def end_and_finish() -> None:
            # One shielded coroutine: a cancellation between the statement and the callbacks would
            # land the one and skip the other.
            nonlocal landed_by_this_call
            deferred_error: Exception | None = None
            span, span_token = await self._hold_transaction_for_ending(give_up_on_timeout=is_commit)
            try:
                if self._finalized:
                    raise TransactionManagementError("Transaction already finalised")
                in_flight = False
                try:
                    deferred_error = await self._before_top_level_end(event)
                    in_flight = True
                    if self._begin_pending:
                        # No statement ran - the driver never sent the BEGIN, and has nothing to end.
                        self._begin_pending = False
                    elif is_commit:
                        await self._driver_commit()
                    else:
                        await self._driver_rollback()
                except BaseException as error:
                    if self._is_connection_lost(error):
                        self._pending_top_level_operation = None
                        if not is_commit:
                            # The server rolls back a transaction whose connection is gone.
                            self.log.warning("Connection lost while rolling back transaction %s", self.connection_name)
                            landed_by_this_call = True
                            await self._finish_after_lost_connection(error, commit_outcome_unknown=False)
                            return
                        await self._finish_after_lost_connection(
                            error, commit_outcome_unknown=in_flight and self._is_commit_outcome_unknown(error)
                        )
                    elif is_commit and self._is_commit_rejection(error):
                        self._pending_top_level_operation = None
                        await self._after_rejected_commit(error)
                        await self._finish_after_failed_commit(error)
                    raise
                landed_by_this_call = True
                self._finalized = True
                self._pending_top_level_operation = None
            finally:
                self._stop_holding_transaction(span, span_token)
            await self._finish_top_level_operation(event)
            if deferred_error is not None:
                raise deferred_error

        try:
            await self._run_shielded_from_cancellation(
                end_and_finish(), on_landed=self._do_nothing, on_timeout=self._mark_top_level_operation_abandoned
            )
        except BaseException as error:
            if landed_by_this_call or not self._is_transaction_finished_error(error):
                raise
            # An earlier, interrupted call already ended the transaction - its callbacks run now.
            self._finalized = True
            self._pending_top_level_operation = None
            if previous_pending is not None:
                await self._run_shielded_from_cancellation(
                    self._finish_top_level_operation(previous_pending), on_landed=self._do_nothing
                )

    async def stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Any]]:
        """Runs ``query`` on a server-side cursor, yielding the raw rows a batch at a time as they
        arrive. Appends the query tags, runs the query wrappers around the whole iteration and
        reports one ``QueryExecuted`` once the stream is read to the end or closed.

        Args:
            query: The SQL.
            values: The bound values.
            chunk_size: How many rows a batch holds - the driver's own default when 0.
        """
        query = QueryTags.append(query)
        start = time.monotonic()
        error: Exception | None = None
        if Observers.query_wrappers:
            call = QueryCall("stream", query, values, self.connection_name, self.dialect)
            batches = Observers.stream_wrapped(call, lambda: self._driver_stream_batches(query, values, chunk_size))
        else:
            batches = self._driver_stream_batches(query, values, chunk_size)
        try:
            async for batch in batches:
                yield batch
        except Exception as exc:
            error = exc
            raise
        finally:
            try:
                if (close_batches := getattr(batches, "aclose", None)) is not None:
                    await close_batches()
            finally:
                Observers.record_query(query, values, start, error, self.connection_name)

    async def stream(self, query: str, values: list[Any] | None = None, chunk_size: int = 0) -> AsyncGenerator[Any]:
        """``stream_batches()`` a row at a time - reading a row after the transaction ended raises.

        Args:
            query: The SQL.
            values: The bound values.
            chunk_size: How many rows to fetch per round trip - the driver's own default when 0.
        """
        async with contextlib.aclosing(self.stream_batches(query, values, chunk_size)) as batches:
            async for batch in batches:
                for row in batch:
                    self.check_stream_open()
                    yield row

    def check_stream_open(self) -> None:
        """Refuses to hand out a streamed row once the transaction has ended or is ending - a row
        fetched before that is no longer read.

        Raises:
            TransactionManagementError: The transaction ended or is ending.
        """
        top_level_transaction = self._get_top_level_transaction()
        if self._finalized or top_level_transaction._finalized or top_level_transaction._ending:
            self._check_statement_allowed()

    async def _driver_stream_batches(
        self, query: str, values: list[Any] | None = None, chunk_size: int = 0
    ) -> AsyncGenerator[list[Any]]:
        """The driver's server-side cursor behind ``stream_batches()`` - implemented by a driver that
        supports streaming.

        Args:
            query: The tagged SQL.
            values: The bound values.
            chunk_size: How many rows a batch holds - the driver's own default when 0.
        """
        raise UnSupportedError(f"{self.dialect.name} has no server-side streaming support")
        yield  # type: ignore[unreachable]  # pragma: nocoverage - makes this an async generator.

    @staticmethod
    async def _run_callbacks(callbacks: list[Callable[[], Any]], label: str) -> None:
        """Runs every callback even if one fails.

        Args:
            callbacks: The callbacks, in registration order.
            label: The callback type for the ExceptionGroup message
                (``"on_commit()"``/``"on_rollback()"``).

        Raises:
            Exception: The one callback failure.
            ExceptionGroup: Every failure, when two or more callbacks failed.
        """
        errors: list[Exception] = []
        for callback in callbacks:
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup(f"{label} callback failures", errors)

    async def _release_transaction_resources(self) -> None:
        """Gives the transaction's connection (or connection lock) back once the top-level
        COMMIT/ROLLBACK landed, before any callback runs. Idempotent.
        """
        context = self._transaction_context_ref() if self._transaction_context_ref is not None else None
        if context is not None:
            await context._release_resources()

    async def _run_callbacks_as_outer_level(self, callbacks: list[Callable[[], Any]], label: str) -> None:
        """Runs the callbacks of a finished top-level transaction with this alias pointing back at
        the client the transaction was opened from, so they can query, open new transactions and
        register further on_commit() callbacks like any code outside a transaction.

        Args:
            callbacks: the callbacks to run.
            label: names the callback type in an ExceptionGroup message.
        """
        handler = Connections.current()
        token = handler.set(self.connection_name, self._parent)
        try:
            await self._run_callbacks(callbacks, label)
        finally:
            handler.reset(token)

    async def _finish_top_level_operation(
        self, event: TransactionEventType, *, run_callbacks: bool = True, cause: Exception | None = None
    ) -> None:
        """Completes a real top-level COMMIT/ROLLBACK that has just landed: hands the
        transaction's resources back, runs the matching callbacks as the outer level and records
        the instrumentation event whatever the callbacks did.

        Args:
            event: ``COMMIT`` or ``ROLLBACK``.
            run_callbacks: False discards every callback without running it - for a transaction
                whose outcome is unknown.
            cause: the error that ended the transaction, reported in its ``TransactionEvent``.
        Raises:
            Exception: the single callback failure, or an ExceptionGroup of several.
        """
        try:
            try:
                await self._release_transaction_resources()
            except Exception as release_error:
                self._release_failure = release_error
            # A real, top-level commit means nothing was ever undone - the on_rollback() callbacks
            # are moot and discarded, never fired (and the other way round for a rollback).
            commit_entries, self._on_commit_callbacks = self._on_commit_callbacks, []
            rollback_entries, self._on_rollback_callbacks = self._on_rollback_callbacks, []
            if not run_callbacks:
                return
            if event is TransactionEventType.COMMIT:
                if commit_entries:
                    await self._run_callbacks_as_outer_level(
                        [entry.callback for entry in commit_entries], "on_commit()"
                    )
            elif rollback_entries:
                await self._run_callbacks_as_outer_level(
                    [entry.callback for entry in rollback_entries], "on_rollback()"
                )
        finally:
            self._record_transaction_end(event, cause)

    async def _finish_after_failed_commit(self, commit_error: BaseException) -> None:
        """Finishes a top-level transaction whose COMMIT was rejected: it is rolled back, so the
        ``on_rollback()`` callbacks run. ``commit_error`` stays the exception raised - a failing
        callback is logged and attached to it as a note.

        Args:
            commit_error: The exception the COMMIT raised.
        """
        self._finalized = True
        try:
            await self._finish_top_level_operation(TransactionEventType.ROLLBACK)
        except Exception as callback_error:
            self.log.error("on_rollback() callback failed after a rejected COMMIT", exc_info=callback_error)
            commit_error.add_note(f"on_rollback() callback failed after the rejected COMMIT: {callback_error!r}")

    async def _finish_after_lost_connection(self, error: BaseException, *, commit_outcome_unknown: bool) -> None:
        """Completes a top-level transaction whose connection was lost before its COMMIT/ROLLBACK
        could land: the server rolls such a transaction back, so it is finalized and reported as
        a ROLLBACK. A failing callback is logged and attached to ``error`` as a note.

        Args:
            error: the connection error, which stays the exception that propagates.
            commit_outcome_unknown: True when the connection broke while a COMMIT was in flight -
                it may have landed, so no callback runs and the ROLLBACK event carries ``error``.
        """
        self._finalized = True
        cause = error if isinstance(error, Exception) else None
        try:
            await self._finish_top_level_operation(
                TransactionEventType.ROLLBACK, run_callbacks=not commit_outcome_unknown, cause=cause
            )
        except Exception as callback_error:
            self.log.error("on_rollback() callback failed after the connection was lost", exc_info=callback_error)
            error.add_note(f"on_rollback() callback failed after the connection was lost: {callback_error!r}")

    async def _discard_rolled_back_savepoint_callbacks(self) -> None:
        """After this savepoint's ROLLBACK TO landed: drops the ``on_commit()`` callbacks registered
        inside it and runs its ``on_rollback()`` callbacks. Callbacks of other levels are left
        alone.

        Raises:
            Exception: The one failing callback's exception, or an ExceptionGroup of several.
        """
        savepoint_span = self._savepoint_span
        if savepoint_span is None:
            return
        top_level = self._get_top_level_transaction()
        top_level._on_commit_callbacks = [
            entry for entry in top_level._on_commit_callbacks if not entry.is_registered_within(savepoint_span)
        ]
        fired_entries = [
            entry for entry in top_level._on_rollback_callbacks if entry.is_registered_within(savepoint_span)
        ]
        top_level._on_rollback_callbacks = [
            entry for entry in top_level._on_rollback_callbacks if not entry.is_registered_within(savepoint_span)
        ]
        await self._run_callbacks([entry.callback for entry in fired_entries], "on_rollback()")

    async def _hold_transaction_for_ending(
        self, *, give_up_on_timeout: bool
    ) -> tuple[SavepointSpan | None, contextvars.Token[SavepointSpan | None] | None]:
        """Marks this top-level transaction as ending, waits until no statement or savepoint of another
        task is open on it and holds it until ``_stop_holding_transaction()`` - the COMMIT/ROLLBACK
        never lands while a sibling's statement is on the wire, and none starts after it.

        Args:
            give_up_on_timeout: Raise when the wait times out - True for a COMMIT; a ROLLBACK goes
                ahead anyway.

        Returns:
            The span held and the token restoring the current span - both for
            ``_stop_holding_transaction()``.

        Raises:
            TransactionManagementError: The wait timed out and ``give_up_on_timeout`` is set.
        """
        self._ending = True
        savepoint_lock = self._savepoint_lock
        parent_span = savepoint_lock.get_own_ancestor_span(current_savepoint_span.get())
        span: SavepointSpan | None
        try:
            span = await savepoint_lock.acquire(parent_span, timeout_seconds=TRANSACTION_END_WAIT_TIMEOUT_SECONDS)
        except TimeoutError:
            if give_up_on_timeout:
                raise TransactionManagementError(
                    "Nothing was committed: a concurrent asyncio.gather()/TaskGroup task still runs a "
                    "statement or holds a savepoint of this transaction after "
                    f"{TRANSACTION_END_WAIT_TIMEOUT_SECONDS:.0f}s - let every task working on a "
                    "transaction finish before the transaction ends."
                ) from None
            self.log.warning("Rolling back transaction %s while another task still uses it", self.connection_name)
            span = None
        span_token = current_savepoint_span.set(span) if span is not None else None
        self._ending_span = span
        return span, span_token

    def _stop_holding_transaction(
        self, span: SavepointSpan | None, span_token: contextvars.Token[SavepointSpan | None] | None
    ) -> None:
        """Ends what ``_hold_transaction_for_ending()`` holds.

        Args:
            span: The span it holds.
            span_token: The token restoring the current span.
        """
        self._ending_span = None
        if span_token is not None:
            current_savepoint_span.reset(span_token)
        if span is not None:
            self._savepoint_lock.release(span)

    @staticmethod
    async def _run_shielded_from_cancellation(
        coroutine: Awaitable[Any],
        *,
        on_landed: Callable[[], None],
        on_timeout: Callable[[asyncio.Future[Any]], None] | None = None,
    ) -> Any:
        """Runs ``coroutine`` to completion even if the awaiting task is cancelled, calls ``on_landed``
        once it finished without error, and only then lets the cancellation through - a
        COMMIT/ROLLBACK can land on the server after its task was cancelled, and the bookkeeping
        must agree with the database.

        After a cancellation the wait is bounded (``SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS``): a
        dead server must not hang it. When the bound is hit the task is left running, ``on_timeout``
        gets it - to keep a resource the task may still use - and the cancellation goes through.

        A coroutine starts eagerly, up to its first suspension. Returns the coroutine's result.
        """
        if asyncio.iscoroutine(coroutine):
            task: asyncio.Future[Any] = asyncio.Task(coroutine, loop=asyncio.get_running_loop(), eager_start=True)
            if task.done():
                # Finished without suspending - nothing for a cancellation to interrupt.
                result = task.result()
                on_landed()
                return result
        else:
            task = asyncio.ensure_future(coroutine)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await TransactionClient._wait_through_cancellations(task, on_timeout)
            raise
        finally:
            if task.done() and not task.cancelled() and task.exception() is None:
                on_landed()

    @staticmethod
    async def _wait_through_cancellations(
        task: "asyncio.Future[Any]", on_timeout: Callable[[asyncio.Future[Any]], None] | None
    ) -> None:
        """Waits for a shielded ``task`` after the awaiting task was cancelled, ignoring any number
        of further cancellations, bounded by SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS.

        Args:
            task: the shielded operation.
            on_timeout: called with ``task`` when the bound is hit while it still runs.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS
        while not task.done():
            remaining_seconds = deadline - loop.time()
            if remaining_seconds <= 0:
                if on_timeout is not None:
                    on_timeout(task)
                return
            try:
                await asyncio.wait([task], timeout=remaining_seconds)
            except asyncio.CancelledError:
                continue

    def _record_transaction_end(self, event: TransactionEventType, cause: Exception | None = None) -> None:
        """Reports a top-level COMMIT/ROLLBACK to the observers - never a savepoint's."""
        started_at = self._transaction_started_at
        if started_at is None:
            return
        duration_ms = (time.monotonic() - started_at) * 1000
        Observers.record_transaction(event, self.connection_name, duration_ms, cause)

    async def _prepare_transaction(self, xid: str) -> None:
        """Ends the transaction's local block with ``PREPARE TRANSACTION`` - for
        ``Transactions.distributed()``. The writes become permanent once ``COMMIT PREPARED`` runs,
        from any connection. No callback runs and no event is reported here: the outcome isn't known
        yet.

        Raises:
            TransactionManagementError: The transaction is already finished.
            UnSupportedError: The dialect has no two-phase commit.
        """
        if self._finalized:
            raise TransactionManagementError("Transaction already finalised")
        two_phase_commit = self.dialect.two_phase_commit
        if two_phase_commit is None:
            raise UnSupportedError(f"The {self.dialect} dialect has no two-phase commit")
        # Shielded: a PREPARE landing after its task was cancelled must mark this client prepared,
        # or cleanup would issue a plain ROLLBACK and orphan the prepared transaction. The GID is
        # rendered as a literal - the statement takes no parameter.
        prepare_sql = two_phase_commit.get_prepare_sql(self.dialect.get_string_literal_sql(xid))
        await self._run_shielded_from_cancellation(self.execute(prepare_sql), on_landed=self._mark_prepared)

    def _mark_prepared(self) -> None:
        self._finalized = True
        self._prepared_without_release = True
