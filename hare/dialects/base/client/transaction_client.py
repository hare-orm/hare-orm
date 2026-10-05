from __future__ import annotations

import abc
import asyncio
import weakref
from typing import Any, ClassVar

from hare.dialects.base.client.constants import TRANSACTION_FINALISED_MESSAGE
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.dialects.base.transactions.contexts.joined_transaction_context import JoinedTransactionContext
from hare.dialects.base.transactions.contexts.nested_transaction_context import NestedTransactionContext
from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext
from hare.dialects.base.transactions.savepoints.nested_savepoint_lock import NestedSavepointLock
from hare.dialects.base.transactions.savepoints.savepoint_span import SavepointSpan, current_savepoint_span
from hare.dialects.base.transactions.transaction_callback import TransactionCallback
from hare.exceptions import (
    QueryError,
    TransactionManagementError,
    UnSupportedError,
)
from hare.instrumentation.declarations import PoolStatus
from hare.transactions.enums import TransactionEventType
from hare.transactions.transaction_options import TransactionOptions


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
    _shielded_savepoint_abandoned_task: asyncio.Future[Any] | None = None
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
    _ending_span: SavepointSpan | None = None
    #: Serializes this transaction's statements and savepoints across tasks - one instance per
    #: top-level transaction, shared by every nested wrapper (created by each backend's wrapper).
    _savepoint_lock: NestedSavepointLock
    #: The span of the savepoint this nested wrapper opened - set by NestedTransactionContext, None
    #: on a top-level wrapper.
    _savepoint_span: SavepointSpan | None = None
    #: Set on a nested wrapper from savepoint() until a statement run under it sends the SAVEPOINT -
    #: a nested block running no statement sends nothing.
    _savepoint_pending: bool = False
    #: The nested wrappers of the transaction whose SAVEPOINT is still to be sent, outermost first -
    #: kept on the top-level wrapper.
    _pending_savepoints: list[TransactionClient]
    #: Set on the top-level wrapper of a driver that sends its BEGIN with the transaction's first
    #: statement (``_driver_send_begin()``), from begin() until a statement sends it - a block
    #: running no statement sends nothing.
    _begin_pending: bool = False
    #: Set on the top-level wrapper once an error left a nested block joined to the transaction - on
    #: a database without savepoints the block's statements can't be undone alone: the transaction
    #: rolls back instead of committing. The error, None while the transaction may commit.
    _rollback_only_error: BaseException | None = None

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
        self.connection_alias = connection.connection_alias
        self.fetch_inserted = connection.fetch_inserted
        self.features = connection.features
        self.tenant_schema_template = connection.tenant_schema_template
        self.tenant_schema = connection.tenant_schema
        self.tenant_client_settings = connection.tenant_client_settings
        self.tenant_row_level_security = connection.tenant_row_level_security
        if isinstance(connection, TransactionClient):
            self.tenant_scope = connection.tenant_scope
            self.sets_tenants = connection.sets_tenants
        self._savepoint_lock = savepoint_lock if savepoint_lock is not None else NestedSavepointLock()
        #: This wrapper's own savepoint name - set only on a nested wrapper, by savepoint().
        self._savepoint_name: str | None = None
        self._finalized = False
        self._on_commit_callbacks = []
        self._on_rollback_callbacks = []
        self._pending_savepoints = []
        #: Why a commit of this top-level transaction is refused - set on a transaction isolating a
        #: test (``RollbackIsolation``), which only ever rolls back.
        self._commit_refusal: str | None = None

    def _in_transaction(self, options: TransactionOptions = TransactionOptions.DEFAULT) -> TransactionContext:
        self._check_nested_transaction_options(options)
        if not self.features.supports_savepoints:
            return JoinedTransactionContext(self)
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
        while client.is_transaction_client:
            client = client._parent
        return client

    def get_pool_status(self) -> PoolStatus | None:
        """The status of the pool the transaction took its connection from."""
        return self.get_non_transactional_client().get_pool_status()

    def get_tenant_client(self) -> DatabaseClient:
        """The transaction itself - it runs in the schema of the tenant active when it began, and
        with ``tenant_row_level_security`` sees the rows of the tenants active when it began.

        Raises:
            QueryError: The active tenant scope is of another tenant schema than the transaction's,
                or - with ``tenant_row_level_security`` - another scope than the one it began in.
        """
        # Local imports: the tenant modules import the models, which import the dialects.
        if self.tenant_schema_template is not None:
            from hare.models.tenancy.tenant_schemas import TenantSchemas

            schema_name = TenantSchemas.get_active_schema_name(self)
            if schema_name != self.tenant_schema:
                raise QueryError(
                    f"The transaction on {self.connection_alias!r} runs in the schema "
                    f"{self.tenant_schema or 'of no tenant'!r} - a query in it can't switch to "
                    f"{schema_name or 'no tenant'!r}; open the transaction inside the tenant scope"
                )
        if self.tenant_row_level_security:
            from hare.models.tenancy.tenancy import Tenancy

            scope = Tenancy.current.get()
            if scope is not self.tenant_scope and scope != self.tenant_scope:
                raise QueryError(
                    f"The transaction on {self.connection_alias!r} sees the rows of the tenant scope "
                    f"{self.tenant_scope!r} it began in - a query in it can't switch to {scope!r}; open the "
                    "transaction inside the tenant scope"
                )
        return self

    def _get_top_level_transaction(self) -> TransactionClient:
        """The outermost transaction wrapper this one (a savepoint, or itself) belongs to.

        Returns:
            The top-level transaction wrapper.
        """
        outer_transaction = self._outer_transaction
        return self if outer_transaction is None else outer_transaction

    def _mark_statement_failed(self) -> None:
        """Records on the top-level wrapper that a statement of this transaction failed."""
        self._get_top_level_transaction()._statement_failed = True

    def _mark_rollback_only(self, error: BaseException) -> None:
        """Has the transaction roll back instead of committing - an error left a nested block joined
        to it.

        Args:
            error: The error.
        """
        top_level_transaction = self._get_top_level_transaction()
        if top_level_transaction._rollback_only_error is None:
            top_level_transaction._rollback_only_error = error

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

    def _mark_savepoint_operation_abandoned(self, task: asyncio.Future[Any]) -> None:
        """``on_timeout`` callback for a savepoint-level (RELEASE/ROLLBACK TO) shielded call -
        see ``_shielded_operation_abandoned``/``_shielded_savepoint_abandoned_task``'s own
        docstrings for what each half is read by."""
        self._shielded_operation_abandoned = True
        self._shielded_savepoint_abandoned_task = task

    #: time.perf_counter() at the top-level BEGIN - the transaction's duration is computed from it.
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
    _transaction_context_reference: weakref.ref[TransactionContext] | None = None
    #: The error a failed ``TransactionEnding.release_transaction_resources()`` raised, kept for the owning
    #: context's __aexit__ to report - it knows whether the block's own exception is already in
    #: flight, which a failed release must never replace.
    _release_failure: Exception | None = None
    #: How this transaction runs - set on the top-level wrapper by ``_in_transaction(options)``
    #: and inherited by every nested one.
    _transaction_options: TransactionOptions = TransactionOptions.DEFAULT
    #: With ``tenant_row_level_security``: the tenant scope the transaction began in, and whether it
    #: set its tenants for the ``TenantCondition`` policies - not under no scope or a scope given
    #: model by model. A nested transaction takes its outer one's.
    tenant_scope: Any = None
    sets_tenants: bool = False

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
        if options.statement_timeout is not None or options.lock_timeout is not None:
            raise QueryError(
                "statement_timeout and lock_timeout can only be set on the outermost transaction - a "
                "nested transaction runs with the timeouts of the transaction it is nested in."
            )
        if options.isolation is not None and options.isolation != self._transaction_options.isolation:
            raise QueryError(
                f"A nested transaction can't run at the {options.isolation!s} isolation level - it runs at "
                "the isolation level of the transaction it is nested in; set isolation on the outermost one."
            )

    async def _apply_transaction_restrictions(self) -> None:
        """Makes the just-begun top-level transaction run at its isolation level, read-only and/or
        time-limited, through the ordinary query path so every statement is observed."""
        statements = self._get_transaction_restriction_statements(self._transaction_options)
        if self.tenant_row_level_security:
            # Local import: the tenant modules import the models, which import the dialects.
            from hare.models.tenancy.tenant_row_level_security import TenantRowLevelSecurity

            self.tenant_scope, tenant_setting_sql = TenantRowLevelSecurity.get_transaction_setting(self)
            if tenant_setting_sql is not None:
                statements.append(tenant_setting_sql)
                self.sets_tenants = True
        for statement in statements:
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

    def _mark_top_level_operation_abandoned(self, task: asyncio.Future[Any]) -> None:
        self._shielded_operation_abandoned = True

    @staticmethod
    def _do_nothing() -> None:
        """``on_landed`` of a shielded call that has nothing to record."""

    async def begin(self) -> None:
        # Shielded: the BEGIN can land after the awaiting task was cancelled, and the connection
        # would then go back to the pool mid-transaction.
        await TransactionEnding.run_shielded_from_cancellation(self._driver_begin(), on_landed=self._do_nothing)

    async def savepoint(self) -> None:
        self._check_savepoint_allowed()
        self._savepoint_name = self._get_new_savepoint_name()
        # The SAVEPOINT goes out ahead of the first statement run under it
        # (PendingStatements.send_pending_savepoints()).
        self._savepoint_pending = True
        self._get_top_level_transaction()._pending_savepoints.append(self)

    def _mark_begin_sent(self) -> None:
        self._begin_pending = False

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
            raise TransactionManagementError(TRANSACTION_FINALISED_MESSAGE)
        self._check_savepoint_allowed()
        if self._savepoint_pending:
            # No statement ran under it - nothing was sent, nothing is released.
            self._drop_pending_savepoint()
            self._mark_finalized()
            return
        # Not a real commit - the on_commit() callbacks only run once the outermost level commits.
        await TransactionEnding.run_shielded_from_cancellation(
            self._driver_release_savepoint(savepoint_name),
            on_landed=self._mark_finalized,
            on_timeout=self._mark_savepoint_operation_abandoned,
        )

    async def savepoint_rollback(self) -> None:
        savepoint_name = self._savepoint_name
        if savepoint_name is None:
            raise TransactionManagementError("Transaction is in invalid state")
        if self._is_transaction_finished():
            raise TransactionManagementError(TRANSACTION_FINALISED_MESSAGE)

        async def roll_back_and_fire_callbacks() -> None:
            # One shielded coroutine: a cancellation between the ROLLBACK TO and the callbacks
            # would land the one and skip the other. A savepoint no statement ran under has
            # nothing to roll back - nothing was sent.
            if self._savepoint_pending:
                self._drop_pending_savepoint()
            else:
                await self._driver_rollback_to_savepoint(savepoint_name)
            self._finalized = True
            await TransactionCallbacks.discard_rolled_back_savepoint_callbacks(self)

        await TransactionEnding.run_shielded_from_cancellation(
            roll_back_and_fire_callbacks(),
            on_landed=self._do_nothing,
            on_timeout=self._mark_savepoint_operation_abandoned,
        )

    async def commit(self) -> None:
        if self._commit_refusal is not None and self._savepoint_name is None:
            raise TransactionManagementError(self._commit_refusal)
        rollback_only_error = self._get_top_level_transaction()._rollback_only_error
        if rollback_only_error is not None and self._savepoint_name is None:
            await self.rollback()
            raise TransactionManagementError(
                f"The transaction was rolled back - a nested atomic() block raised {rollback_only_error!r}, and "
                f"{self.dialect} transactions have no savepoints to undo it alone"
            ) from rollback_only_error
        await self._check_commit_allowed()
        # A nested wrapper shares the transaction - its commit releases its savepoint.
        if self._savepoint_name is not None:
            await self.release_savepoint()
            return
        await TransactionEnding.end_top_level(self, TransactionEventType.COMMIT)

    async def rollback(self) -> None:
        if self._savepoint_name is not None:
            await self.savepoint_rollback()
            return
        await TransactionEnding.end_top_level(self, TransactionEventType.ROLLBACK)

    def check_stream_open(self) -> None:
        """Refuses to hand out a streamed row once the transaction has ended or is ending - a row
        fetched before that is no longer read.

        Raises:
            TransactionManagementError: The transaction ended or is ending.
        """
        top_level_transaction = self._get_top_level_transaction()
        if self._finalized or top_level_transaction._finalized or top_level_transaction._ending:
            self._check_statement_allowed()

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
            raise TransactionManagementError(TRANSACTION_FINALISED_MESSAGE)
        two_phase_commit = self.dialect.two_phase_commit
        if two_phase_commit is None:
            raise UnSupportedError(f"The {self.dialect} dialect has no two-phase commit")
        # Shielded: a PREPARE landing after its task was cancelled must mark this client prepared,
        # or cleanup would issue a plain ROLLBACK and orphan the prepared transaction. The GID is
        # rendered as a literal - the statement takes no parameter.
        prepare_sql = two_phase_commit.get_prepare_sql(self.dialect.literals.get_string_literal_sql(xid))
        await TransactionEnding.run_shielded_from_cancellation(
            self.execute(prepare_sql), on_landed=self._mark_prepared
        )

    def _mark_prepared(self) -> None:
        self._finalized = True
        self._prepared_without_release = True
