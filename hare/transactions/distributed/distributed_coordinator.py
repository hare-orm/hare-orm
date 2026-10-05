from __future__ import annotations

import asyncio
import string
import uuid
from collections.abc import AsyncGenerator, Collection, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from hare.core.connections.connections import Connections
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from hare.exceptions import (
    ConfigurationError,
    DistributedTransactionCommitAmbiguousError,
    DistributedTransactionPartiallyCommittedError,
    OperationalError,
    QueryError,
    UnSupportedError,
)
from hare.transactions.atomic.atomic import Atomic
from hare.transactions.constants import (
    DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS,
    DISTRIBUTED_TRANSACTION_XID_PREFIX,
    DISTRIBUTED_TRANSACTION_XID_UNIQUE_PART_LENGTH,
)
from hare.transactions.enums import DistributedTransactionResolution, TransactionEventType

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext
    from hare.dialects.base.transactions.two_phase_commit import TwoPhaseCommit
from hare.transactions.distributed.declarations import OpenDistributedTransaction
from hare.transactions.distributed.distributed_transactions import DistributedTransactions
from hare.transactions.distributed.stale_prepared_transaction import StalePreparedTransaction


class DistributedCoordinator:
    """Two-phase commit across several databases: prepares every participant, records the decision
    on the coordinator and finishes or recovers the prepared transactions."""

    @staticmethod
    def _require_distributed_capability(connection: DatabaseClient, connection_alias: str) -> None:
        """Rejects a connection ``Transactions.distributed()`` can't run a two-phase commit on.

        Args:
            connection: The connection_alias's connection.
            connection_alias: The connection connection_alias.

        Raises:
            UnSupportedError: The connection's database or driver has no two-phase commit.
        """
        if not connection.features.supports_two_phase_commit or connection.dialect.two_phase_commit is None:
            raise UnSupportedError(
                f"Transactions.distributed() needs two-phase commit on every alias - {connection_alias!r} is "
                f"{connection.dialect.name}, which has no way to prepare a transaction and commit it later."
            )

    @staticmethod
    def _get_two_phase_commit(connection: DatabaseClient) -> TwoPhaseCommit:
        """Returns the two-phase commit statements of a connection's dialect.

        Args:
            connection: A connection ``_require_distributed_capability`` accepts.

        Returns:
            The statements.

        Raises:
            UnSupportedError: The connection's dialect has no two-phase commit.
        """
        two_phase_commit = connection.dialect.two_phase_commit
        if two_phase_commit is None:
            raise UnSupportedError(f"The {connection.dialect} dialect has no two-phase commit")
        return two_phase_commit

    @staticmethod
    async def _ensure_distributed_decisions_table(client: DatabaseClient) -> None:
        """Creates the decision log table on the coordinator unless it exists.

        Args:
            client: A connection to the coordinator alias.
        """
        await client.execute(DistributedCoordinator._get_two_phase_commit(client).get_decisions_table_sql())

    @staticmethod
    async def mark_decision_resolved(client: DatabaseClient, xid: str) -> None:
        """Marks a decision resolved - every participant of ``xid`` committed.

        Args:
            client: A connection to the coordinator alias.
            xid: The logical xid.
        """
        await client.execute(DistributedCoordinator._get_two_phase_commit(client).get_finish_decision_sql(), [xid])

    @staticmethod
    def _make_xid(coordinator: str) -> str:
        """The logical xid of a new ``distributed()`` call, embedding the coordinator alias - recovery
        leaves a prepared transaction of another coordinator sharing a participant alone.
        """
        return f"{DISTRIBUTED_TRANSACTION_XID_PREFIX}{coordinator}:{uuid.uuid4().hex}"

    @staticmethod
    def _xid_belongs_to_coordinator(xid: str, coordinator: str) -> bool:
        """Whether ``xid`` is exactly an xid ``_make_xid(coordinator)`` builds - the whole
        coordinator alias followed by one uuid4 hex, so coordinators ``a`` and ``a:b`` (or a
        participant GID of alias ``x:db`` read as one of alias ``db``) aren't confused.

        Args:
            xid: The logical xid.
            coordinator: The coordinator alias.

        Returns:
            True when ``xid`` belongs to ``coordinator``.
        """
        coordinator_prefix = f"{DISTRIBUTED_TRANSACTION_XID_PREFIX}{coordinator}:"
        if not xid.startswith(coordinator_prefix):
            return False
        unique_part = xid.removeprefix(coordinator_prefix)
        return len(unique_part) == DISTRIBUTED_TRANSACTION_XID_UNIQUE_PART_LENGTH and all(
            character in string.hexdigits for character in unique_part
        )

    @staticmethod
    def _get_participant_gid_like_pattern(connection_alias: str, escape_character: str) -> str:
        """The LIKE pattern matching every hare GID prepared for participant ``connection_alias``, with
        ``%``/``_`` in the prefix and the connection_alias matched literally.

        Args:
            connection_alias: The participant connection_alias.
            escape_character: The pattern's escape character (``TwoPhaseCommit.like_escape_character``).

        Returns:
            The pattern.
        """
        literal_parts = [DISTRIBUTED_TRANSACTION_XID_PREFIX, f":{connection_alias}"]
        escaped_parts = [
            part.replace(escape_character, escape_character * 2)
            .replace("%", f"{escape_character}%")
            .replace("_", f"{escape_character}_")
            for part in literal_parts
        ]
        return "%".join(escaped_parts)

    @staticmethod
    def _participant_gid(xid: str, connection_alias: str) -> str:
        """The GID a participant's transaction is prepared under - the xid plus the connection_alias: a GID is
        unique per server, not per database, so participants on one server would collide on the bare
        xid.
        """
        return f"{xid}:{connection_alias}"

    @staticmethod
    async def _prepare_participant(client: TransactionClient, connection_alias: str, xid: str) -> None:
        """Prepares a participant's transaction under its GID.

        Args:
            client: The participant's transaction client.
            connection_alias: The participant connection_alias.
            xid: The logical xid.

        Raises:
            ConfigurationError: The database's configuration keeps it from preparing - prepared
                transactions switched off, or every slot for them taken.
        """
        try:
            await client._prepare_transaction(DistributedCoordinator._participant_gid(xid, connection_alias))
        except OperationalError as error:
            failure = DistributedCoordinator._get_two_phase_commit(client).get_prepare_failure(error, connection_alias)
            if failure is not None:
                raise failure from error
            raise

    @staticmethod
    def _get_gid_literal(client: DatabaseClient, connection_alias: str, xid: str) -> str:
        """A participant's GID as an escaped literal - ``COMMIT``/``ROLLBACK PREPARED`` take no bind
        parameter.

        Args:
            client: A connection to the participant connection_alias.
            connection_alias: The participant connection_alias.
            xid: The logical xid.

        Returns:
            The literal.
        """
        return client.dialect.literals.get_string_literal_sql(
            DistributedCoordinator._participant_gid(xid, connection_alias)
        )

    @staticmethod
    async def commit_prepared(client: DatabaseClient, connection_alias: str, xid: str) -> None:
        """Commits a participant's prepared transaction - from any connection to its database.

        Args:
            client: A connection to the participant connection_alias.
            connection_alias: The participant connection_alias.
            xid: The logical xid.
        """
        gid_literal = DistributedCoordinator._get_gid_literal(client, connection_alias, xid)
        await client.execute(DistributedCoordinator._get_two_phase_commit(client).get_commit_prepared_sql(gid_literal))

    @staticmethod
    async def rollback_prepared(client: DatabaseClient, connection_alias: str, xid: str) -> None:
        """Rolls back a participant's prepared transaction - from any connection to its database.

        Args:
            client: A connection to the participant connection_alias.
            connection_alias: The participant connection_alias.
            xid: The logical xid.
        """
        gid_literal = DistributedCoordinator._get_gid_literal(client, connection_alias, xid)
        await client.execute(
            DistributedCoordinator._get_two_phase_commit(client).get_rollback_prepared_sql(gid_literal)
        )

    @staticmethod
    async def _xid_is_prepared(client: DatabaseClient, connection_alias: str, xid: str) -> bool:
        """Returns whether a participant's transaction is prepared in its database.

        Args:
            client: A connection to the participant connection_alias.
            connection_alias: The participant connection_alias.
            xid: The logical xid.

        Returns:
            Whether it is prepared and not yet committed or rolled back.
        """
        gid = DistributedCoordinator._participant_gid(xid, connection_alias)
        _, rows = await client.execute(
            DistributedCoordinator._get_two_phase_commit(client).get_prepared_lookup_sql(), [gid]
        )
        return len(rows) > 0

    @staticmethod
    async def _get_prepared_on_fresh_connection(connection_alias: str, xid: str, *, commit: bool) -> None:
        """Commits or rolls back a prepared transaction on a new connection - the participant's own
        client is already finalized by PREPARE and refuses statements.
        """
        connection = Connections.current().create_independent(connection_alias)
        try:
            if commit:
                await DistributedCoordinator.commit_prepared(connection, connection_alias, xid)
            else:
                await DistributedCoordinator.rollback_prepared(connection, connection_alias, xid)
        finally:
            await connection.close()

    @staticmethod
    async def _roll_back_if_prepared_on_fresh_connection(connection_alias: str, xid: str) -> None:
        """Issues ``ROLLBACK PREPARED`` for a participant whose ``PREPARE TRANSACTION`` outcome is
        unknown (it failed or was interrupted without marking the wrapper prepared) - only if the
        prepared transaction really exists.

        Args:
            connection_alias: The participant connection_alias.
            xid: The distributed transaction's logical xid.
        """
        connection = Connections.current().create_independent(connection_alias)
        try:
            if await DistributedCoordinator._xid_is_prepared(connection, connection_alias, xid):
                await DistributedCoordinator.rollback_prepared(connection, connection_alias, xid)
        finally:
            await connection.close()

    @staticmethod
    async def _roll_back_distributed_transaction(
        participant_contexts: dict[str, TransactionContext],
        participant_clients: dict[str, TransactionClient],
        coordinator_context: TransactionContext,
        coordinator_client: TransactionClient,
        xid: str | None,
        prepare_attempted_aliases: Collection[str] = (),
    ) -> None:
        """The cleanup of every failure path of ``distributed()``: rolls back every participant in
        reverse order - ``ROLLBACK PREPARED`` for a prepared one - then the coordinator, each step
        shielded from the others' exceptions, so a dead connection never stops the cleanup, masks
        the real failure or leaves the coordinator's transaction open. ``xid`` is read only for a
        finalized client - None before one exists. A participant in ``prepare_attempted_aliases``
        not marked prepared may have prepared anyway - ``pg_prepared_xacts`` is checked.
        """
        for connection_alias, client in reversed(participant_clients.items()):
            try:
                if client._finalized:
                    assert xid is not None  # nosec B101
                    await DistributedCoordinator._abort_prepared_participant(connection_alias, client, xid)
                else:
                    await client.rollback()
            except BaseException:  # never let one dead participant block the rest
                client.log.exception(
                    "Failed to roll back distributed transaction participant during cleanup "
                    "(xid=%s, alias=%s) - continuing to clean up the remaining participants "
                    "and the coordinator.",
                    xid,
                    connection_alias,
                )
            if xid is not None and connection_alias in prepare_attempted_aliases and not client._finalized:
                try:
                    await DistributedCoordinator._roll_back_if_prepared_on_fresh_connection(connection_alias, xid)
                except BaseException:  # see above
                    client.log.exception(
                        "Failed to check for a prepared transaction left by an interrupted PREPARE "
                        "during cleanup (xid=%s, alias=%s) - run `hare distributed-recover`.",
                        xid,
                        connection_alias,
                    )
            try:
                await participant_contexts[connection_alias].__aexit__(None, None, None)
            except BaseException:  # see above
                client.log.exception(
                    "Failed to exit the transaction context while cleaning up distributed "
                    "transaction participant (xid=%s, alias=%s).",
                    xid,
                    connection_alias,
                )
        try:
            if not coordinator_client._finalized:
                await coordinator_client.rollback()
        except BaseException:  # see above
            coordinator_client.log.exception(
                "Failed to roll back the distributed transaction coordinator during cleanup (xid=%s).", xid
            )
        try:
            await coordinator_context.__aexit__(None, None, None)
        except BaseException:  # see above
            coordinator_client.log.exception(
                "Failed to exit the coordinator's transaction context while cleaning up "
                "distributed transaction (xid=%s).",
                xid,
            )

    @staticmethod
    async def _abort_prepared_participant(connection_alias: str, client: TransactionClient, xid: str) -> None:
        """Issues ``ROLLBACK PREPARED`` for a prepared participant on a new connection, then finishes
        its transaction like a top-level ``rollback()`` - the connection given back,
        ``on_rollback()`` callbacks run, the ROLLBACK event recorded. A failing callback is logged,
        never masking the reason of the abort.
        """
        await DistributedCoordinator._get_prepared_on_fresh_connection(connection_alias, xid, commit=False)
        try:
            await TransactionEnding.finish_top_level_operation(client, TransactionEventType.ROLLBACK)
        except BaseException:  # see docstring above
            client.log.exception(
                "on_rollback() callback failed while aborting distributed transaction xid=%s (alias=%s)",
                xid,
                connection_alias,
            )

    @staticmethod
    @asynccontextmanager
    async def run(coordinator: str, participants: Sequence[str]) -> AsyncGenerator[DistributedTransactions]:
        """Runs one distributed transaction - what ``Transactions.distributed()`` opens."""
        coordinator_connection, participant_connections = DistributedCoordinator._get_connections(
            coordinator, participants
        )
        coordinator_context = coordinator_connection._in_transaction()
        coordinator_client = await coordinator_context.__aenter__()
        participant_contexts, participant_clients = await DistributedCoordinator._open_participants(
            participant_connections, coordinator_context, coordinator_client
        )

        xid = DistributedCoordinator._make_xid(coordinator)
        try:
            yield DistributedTransactions(coordinator, coordinator_client, participant_clients)
        except BaseException:
            await DistributedCoordinator._roll_back_distributed_transaction(
                participant_contexts, participant_clients, coordinator_context, coordinator_client, xid
            )
            raise

        transaction = OpenDistributedTransaction(
            coordinator,
            participants,
            xid,
            coordinator_context,
            coordinator_client,
            participant_contexts,
            participant_clients,
        )
        await DistributedCoordinator._prepare_and_record_decision(transaction)
        # on_commit() callback failures are kept apart from protocol failures - raised after every
        # commit and cleanup step.
        callback_errors: list[BaseException] = []
        await DistributedCoordinator._commit_coordinator(transaction, callback_errors)
        pending_aliases, cancelled_during_participant_commit = await DistributedCoordinator._commit_participants(
            xid, participant_clients, callback_errors
        )
        await DistributedCoordinator._finish(
            transaction, pending_aliases, cancelled_during_participant_commit, callback_errors
        )

    @staticmethod
    def _get_connections(
        coordinator: str, participants: Sequence[str]
    ) -> tuple[DatabaseClient, dict[str, DatabaseClient]]:
        """The connections of a distributed transaction, each checked to take part in one.

        Args:
            coordinator: The coordinator's connection_alias.
            participants: The participants' aliases.

        Returns:
            The coordinator's connection, and each participant's by connection_alias.

        Raises:
            QueryError: No participant, an connection_alias twice, a connection with no two-phase commit, or one
                already inside a transaction.
        """
        if not participants:
            raise QueryError("Transactions.distributed() needs at least one participant")
        if len(set(participants)) != len(participants) or coordinator in participants:
            raise QueryError(
                f"Transactions.distributed() aliases must be distinct: coordinator={coordinator!r}, "
                f"participants={list(participants)!r}"
            )

        coordinator_connection = Atomic.get_connection(coordinator)
        DistributedCoordinator._require_distributed_capability(coordinator_connection, coordinator)
        participant_connections = {
            connection_alias: Atomic.get_connection(connection_alias) for connection_alias in participants
        }
        for connection_alias, connection in participant_connections.items():
            DistributedCoordinator._require_distributed_capability(connection, connection_alias)

        # An connection_alias already inside a transaction would make a savepoint, not a top-level transaction
        # PREPARE needs - rejected before anything opens.
        for connection_alias, connection in {coordinator: coordinator_connection, **participant_connections}.items():
            if connection.is_transaction_client:
                raise QueryError(
                    f"Transactions.distributed() can't start on {connection_alias!r} - it's already inside "
                    "an open transaction (Transactions.atomic(), or another "
                    "distributed()). Nesting distributed() inside an ambient transaction on the "
                    "same alias isn't supported - run it outside any enclosing transaction."
                )
        return coordinator_connection, participant_connections

    @staticmethod
    async def _open_participants(
        participant_connections: dict[str, DatabaseClient],
        coordinator_context: TransactionContext,
        coordinator_client: TransactionClient,
    ) -> tuple[dict[str, TransactionContext], dict[str, TransactionClient]]:
        """Opens each participant's transaction - every one opened rolled back with the coordinator's
        when one fails to open.

        Args:
            participant_connections: The participants' connections by connection_alias.
            coordinator_context: The coordinator's transaction context.
            coordinator_client: The coordinator's transaction.

        Returns:
            The participants' transaction contexts and transactions by connection_alias.
        """
        participant_contexts: dict[str, TransactionContext] = {}
        participant_clients: dict[str, TransactionClient] = {}
        try:
            for connection_alias, connection in participant_connections.items():
                context = connection._in_transaction()
                participant_contexts[connection_alias] = context
                participant_clients[connection_alias] = await context.__aenter__()
        except BaseException:
            # Reversed: each context set the same ContextVar, and the tokens must be reset
            # last-in-first-out. Nothing is prepared yet - no xid.
            await DistributedCoordinator._roll_back_distributed_transaction(
                participant_contexts, participant_clients, coordinator_context, coordinator_client, None
            )
            raise
        return participant_contexts, participant_clients

    @staticmethod
    async def _prepare_and_record_decision(
        transaction: OpenDistributedTransaction,
    ) -> None:
        """Prepares every participant's transaction, then records the decision to commit in the
        coordinator's transaction - everything rolled back when either fails.

        Args:
            transaction: The distributed transaction.
        """
        prepare_failure: BaseException | None = None
        prepare_attempted_aliases: set[str] = set()
        for connection_alias, client in transaction.participant_clients.items():
            prepare_attempted_aliases.add(connection_alias)
            try:
                await DistributedCoordinator._prepare_participant(client, connection_alias, transaction.xid)
            except BaseException as error:  # re-raised below once everything's rolled back
                prepare_failure = error
                break

        if prepare_failure is not None:
            await DistributedCoordinator._roll_back_distributed_transaction(
                transaction.participant_contexts,
                transaction.participant_clients,
                transaction.coordinator_context,
                transaction.coordinator_client,
                transaction.xid,
                prepare_attempted_aliases,
            )
            raise prepare_failure

        try:
            await DistributedCoordinator._ensure_distributed_decisions_table(transaction.coordinator_client)
            await transaction.coordinator_client.execute(
                DistributedCoordinator._get_two_phase_commit(transaction.coordinator_client).get_decision_insert_sql(),
                [transaction.xid, transaction.coordinator, ",".join(transaction.participants)],
            )
        except BaseException:
            # Nothing durable has happened yet - the coordinator's own transaction (holding the
            # decision-row INSERT) is still open. Safe to roll back every participant's prepared
            # state along with it, exactly like the prepare-failure path above.
            await DistributedCoordinator._roll_back_distributed_transaction(
                transaction.participant_contexts,
                transaction.participant_clients,
                transaction.coordinator_context,
                transaction.coordinator_client,
                transaction.xid,
            )
            raise

    @staticmethod
    async def _commit_coordinator(
        transaction: OpenDistributedTransaction,
        callback_errors: list[BaseException],
    ) -> None:
        """Commits the coordinator's transaction, which holds the decision.

        Args:
            transaction: The distributed transaction.
            callback_errors: The failures of on_commit() callbacks - one of the coordinator's
                appended.

        Raises:
            DistributedTransactionCommitAmbiguousError: Whether the commit landed is unknown.
        """
        try:
            await transaction.coordinator_client.commit()
        except BaseException as error:
            if transaction.coordinator_client._finalized:
                # The COMMIT landed; only a callback failed - the participants still get COMMIT
                # PREPARED.
                callback_errors.append(error)
            else:
                # Whether the COMMIT landed is unknown - the prepared participants are left for
                # `hare distributed-recover`, which reads the decision row. The coordinator's
                # connection is rolled back either way: a ROLLBACK after a landed COMMIT is a no-op,
                # and the connection must not go back to the pool still in a transaction.
                for context in reversed(transaction.participant_contexts.values()):
                    await context.__aexit__(None, None, None)
                try:
                    # The dialect's own rollback statement - rollback() would run on_rollback()
                    # callbacks for what may have committed.
                    await transaction.coordinator_client.execute(
                        transaction.coordinator_client.dialect.transactions.get_rollback_sql()
                    )
                except Exception:  # nosec B110
                    # The connection itself is genuinely broken (not just the ambiguous COMMIT) -
                    # nothing more to do here; _finalized is forced True below regardless so
                    # __aexit__ doesn't try anything against it again.
                    pass
                transaction.coordinator_client._finalized = True
                await transaction.coordinator_context.__aexit__(None, None, None)
                raise DistributedTransactionCommitAmbiguousError(
                    f"Transactions.distributed() (xid={transaction.xid}) failed to confirm the coordinator's "
                    f"own commit - it may or may not have actually landed. Run `hare "
                    f"distributed-recover --coordinator {transaction.coordinator}` to resolve "
                    f"{list(transaction.participants)} definitively.",
                    transaction.xid,
                    transaction.coordinator,
                    list(transaction.participants),
                ) from error

    @staticmethod
    async def _commit_participants(
        xid: str, participant_clients: dict[str, TransactionClient], callback_errors: list[BaseException]
    ) -> tuple[list[str], asyncio.CancelledError | None]:
        """Commits every participant's prepared transaction, each on a connection of its own.

        Args:
            xid: The transaction's id.
            participant_clients: The participants' transactions by connection_alias.
            callback_errors: The failures of on_commit() callbacks - the participants' appended.

        Returns:
            The aliases whose COMMIT PREPARED failed or wasn't reached, and the cancellation that
            stopped the commits, if any.
        """
        pending_aliases: list[str] = []
        cancelled_during_participant_commit: asyncio.CancelledError | None = None
        remaining_participants = iter(participant_clients.items())
        for connection_alias, client in remaining_participants:
            try:
                await DistributedCoordinator._get_prepared_on_fresh_connection(connection_alias, xid, commit=True)
            except asyncio.CancelledError as error:
                # A cancellation stops the loop; this and the remaining participants are reported as
                # unresolved, like a failed COMMIT PREPARED.
                cancelled_during_participant_commit = error
                pending_aliases.append(connection_alias)
                pending_aliases.extend(remaining_alias for remaining_alias, _client in remaining_participants)
                break
            except Exception:
                pending_aliases.append(connection_alias)
                continue
            try:
                # Finished like a top-level commit: the connection given back before the on_commit()
                # callbacks run, then the COMMIT event recorded.
                await TransactionEnding.finish_top_level_operation(client, TransactionEventType.COMMIT)
            except BaseException as error:
                # This participant committed; only a callback failed.
                callback_errors.append(error)
        return pending_aliases, cancelled_during_participant_commit

    @staticmethod
    async def _finish(
        transaction: OpenDistributedTransaction,
        pending_aliases: list[str],
        cancelled_during_participant_commit: asyncio.CancelledError | None,
        callback_errors: list[BaseException],
    ) -> None:
        """Marks the decision resolved once every participant committed, closes the transactions and
        raises what failed.

        Args:
            transaction: The distributed transaction.
            pending_aliases: The participants not committed.
            cancelled_during_participant_commit: The cancellation that stopped the commits.
            callback_errors: The failures of on_commit() callbacks.

        Raises:
            DistributedTransactionPartiallyCommittedError: A participant isn't committed.
        """
        if not pending_aliases:
            # The coordinator's client is finalized - this UPDATE gets its own connection.
            coordinator_followup = Connections.current().create_independent(transaction.coordinator)
            try:
                await DistributedCoordinator.mark_decision_resolved(coordinator_followup, transaction.xid)
            finally:
                await coordinator_followup.close()

        for context in reversed(transaction.participant_contexts.values()):
            await context.__aexit__(None, None, None)
        await transaction.coordinator_context.__aexit__(None, None, None)

        if pending_aliases:
            raise DistributedTransactionPartiallyCommittedError(
                f"Transactions.distributed() (xid={transaction.xid}) committed, but COMMIT PREPARED failed "
                f"for {pending_aliases} - run `hare distributed-recover` to finish delivering "
                "them",
                transaction.xid,
                transaction.coordinator,
                pending_aliases,
            ) from cancelled_during_participant_commit

        if callback_errors:
            # Everything committed; only callbacks failed. BaseExceptionGroup - a callback may raise
            # CancelledError.
            if len(callback_errors) == 1:
                raise callback_errors[0]
            raise BaseExceptionGroup(
                f"Transactions.distributed() (xid={transaction.xid}) committed successfully, but "
                f"{len(callback_errors)} on_commit() callback(s) failed",
                callback_errors,
            )

    @staticmethod
    async def detect_stale_prepared_transactions(
        coordinator: str, older_than_seconds: int = DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS
    ) -> list[StalePreparedTransaction]:
        """Scans for ``Transactions.distributed()`` prepared transactions that never finished
        resolving - the read-only half of ``hare distributed-recover``. Returns what SHOULD
        happen to each (commit vs rollback) without acting on anything.

        Args:
            coordinator: connection_alias holding the ``hare_distributed_decisions`` decision log.
            older_than_seconds: skip anything younger than this - it may still be mid-flight;
                0 skips nothing.

        Raises:
            UnSupportedError: if ``coordinator`` has no two-phase commit.
        """
        coordinator_connection = Atomic.get_connection(coordinator)
        DistributedCoordinator._require_distributed_capability(coordinator_connection, coordinator)
        await DistributedCoordinator._ensure_distributed_decisions_table(coordinator_connection)
        coordinator_two_phase_commit = DistributedCoordinator._get_two_phase_commit(coordinator_connection)
        # No age filter at all for 0: a server clock stepped backwards (NTP, VM time sync) makes a
        # just-written row look younger than zero seconds.
        _, decision_rows = await coordinator_connection.execute(
            coordinator_two_phase_commit.get_pending_decisions_sql(with_age_limit=bool(older_than_seconds)),
            [older_than_seconds] if older_than_seconds else None,
        )

        stale: list[StalePreparedTransaction] = []
        xids_with_decisions: set[str] = set()
        for row in decision_rows:
            xids_with_decisions.add(row["xid"])
            for participant_alias in row["participant_aliases"].split(","):
                try:
                    participant_connection = Atomic.get_connection(participant_alias)
                except ConfigurationError:
                    # An connection_alias recorded in the decision row may be gone from the configuration -
                    # reported as its own entry instead of failing the detection.
                    stale.append(
                        StalePreparedTransaction(
                            xid=row["xid"],
                            participant_alias=participant_alias,
                            resolution=DistributedTransactionResolution.COMMIT,
                            reason=(
                                f"coordinator {coordinator!r} already committed this decision, but "
                                f"connection alias {participant_alias!r} is no longer configured - "
                                "cannot connect to issue COMMIT PREPARED"
                            ),
                        )
                    )
                    continue
                if await DistributedCoordinator._xid_is_prepared(
                    participant_connection, participant_alias, row["xid"]
                ):
                    stale.append(
                        StalePreparedTransaction(
                            xid=row["xid"],
                            participant_alias=participant_alias,
                            resolution=DistributedTransactionResolution.COMMIT,
                            reason=(
                                f"coordinator {coordinator!r} already committed this decision - "
                                "participant still needs COMMIT PREPARED"
                            ),
                        )
                    )

        for connection_alias in Connections.aliases():
            connection = Atomic.get_connection(connection_alias)
            two_phase_commit = connection.dialect.two_phase_commit
            if not connection.features.supports_two_phase_commit or two_phase_commit is None:
                continue
            gid_pattern = DistributedCoordinator._get_participant_gid_like_pattern(
                connection_alias, two_phase_commit.like_escape_character
            )
            _, orphaned_rows = await connection.execute(
                two_phase_commit.get_prepared_listing_sql(with_age_limit=bool(older_than_seconds)),
                [gid_pattern, older_than_seconds] if older_than_seconds else [gid_pattern],
            )
            for row in orphaned_rows:
                logical_xid = row["gid"].removesuffix(f":{connection_alias}")
                if not DistributedCoordinator._xid_belongs_to_coordinator(logical_xid, coordinator):
                    # Prepared by another coordinator - its own recovery run handles it.
                    continue
                if logical_xid in xids_with_decisions:
                    continue
                stale.append(
                    StalePreparedTransaction(
                        xid=logical_xid,
                        participant_alias=connection_alias,
                        resolution=DistributedTransactionResolution.ROLLBACK,
                        reason="no decision row found for this xid - presumed abort",
                    )
                )
        return stale
