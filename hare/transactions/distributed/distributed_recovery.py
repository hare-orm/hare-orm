from __future__ import annotations

from hare.core.connections.connections import Connections
from hare.exceptions import ConfigurationError, TransactionManagementError
from hare.transactions.atomic.atomic import Atomic
from hare.transactions.constants import DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS
from hare.transactions.distributed.declarations import DistributedRecoveryReport, PreparedRecoveryResult
from hare.transactions.distributed.distributed_coordinator import DistributedCoordinator
from hare.transactions.distributed.distributed_decision_store import DistributedDecisionStore
from hare.transactions.distributed.messages import RECOVERY_INSIDE_TRANSACTION
from hare.transactions.distributed.stale_prepared_transaction import StalePreparedTransaction
from hare.transactions.enums import DistributedTransactionResolution


class DistributedRecovery:
    """Shared detection and execution of distributed recovery for API and CLI.

    The decision log determines COMMIT versus presumed abort. Age only controls eligibility,
    never the existence of a decision. Participant failures remain pending; they do not stop
    other participants or allow their decision to be completed prematurely.
    """

    @staticmethod
    async def run(
        coordinator: str,
        *,
        finish: bool = False,
        older_than_seconds: int = DEFAULT_DISTRIBUTED_RECOVERY_OLDER_THAN_SECONDS,
    ) -> DistributedRecoveryReport:
        """Scan current durable decisions, optionally apply them, and report every attempted action.

        No user transaction may surround recovery: prepared commits run outside transactions.
        Cancellation stops further actions; already submitted IO may have completed. A later
        run rechecks the databases rather than replaying a saved report.
        """
        connection = Atomic.get_connection(coordinator)
        DistributedCoordinator._require_distributed_capability(connection, coordinator)
        if finish and connection.is_transaction_client:
            raise TransactionManagementError(RECOVERY_INSIDE_TRANSACTION.format(alias=coordinator))
        store = DistributedDecisionStore(connection)
        entries, ready_xids = await DistributedRecovery._scan(coordinator, store, older_than_seconds)
        if not finish:
            return DistributedRecoveryReport(tuple(entries))

        attempts = []
        pending_xids = set()
        for entry in entries:
            try:
                participant = Atomic.get_connection(entry.participant_alias)
                if participant.is_transaction_client:
                    raise TransactionManagementError(RECOVERY_INSIDE_TRANSACTION.format(alias=entry.participant_alias))
                if entry.resolution is DistributedTransactionResolution.COMMIT:
                    await DistributedCoordinator.commit_prepared(participant, entry.participant_alias, entry.xid)
                else:
                    await DistributedCoordinator.rollback_prepared(participant, entry.participant_alias, entry.xid)
            except Exception as error:
                attempts.append(PreparedRecoveryResult(entry, error))
                pending_xids.add(entry.xid)
            else:
                attempts.append(PreparedRecoveryResult(entry))
                ready_xids.add(entry.xid)

        completed = []
        decision_errors = []
        for xid in sorted(ready_xids - pending_xids):
            try:
                await store.complete(xid)
            except Exception as error:
                decision_errors.append((xid, error))
            else:
                completed.append(xid)
        return DistributedRecoveryReport(tuple(entries), tuple(attempts), tuple(completed), tuple(decision_errors))

    @staticmethod
    async def _scan(
        coordinator: str, store: DistributedDecisionStore, older_than_seconds: int
    ) -> tuple[list[StalePreparedTransaction], set[str]]:
        """Keep all decisions as orphan protection, returning only eligible recovery work."""
        stale = []
        xids_with_decisions = set()
        ready_xids = set()
        for decision in await store.pending(older_than_seconds):
            if not DistributedCoordinator._xid_belongs_to_coordinator(decision.xid, coordinator):
                continue
            xids_with_decisions.add(decision.xid)
            if not decision.is_stale:
                continue
            prepared = False
            for alias in decision.participants:
                try:
                    connection = Atomic.get_connection(alias)
                except ConfigurationError:
                    reason = (
                        f"coordinator {coordinator!r} already committed this decision, but connection alias "
                        f"{alias!r} is no longer configured - cannot connect to commit its prepared transaction"
                    )
                else:
                    if not await DistributedCoordinator._xid_is_prepared(connection, alias, decision.xid):
                        continue
                    reason = (
                        f"coordinator {coordinator!r} already committed this decision - "
                        "the participant's prepared transaction still needs its commit"
                    )
                prepared = True
                stale.append(
                    StalePreparedTransaction(decision.xid, alias, DistributedTransactionResolution.COMMIT, reason)
                )
            if not prepared:
                # Recovery may have crashed after the final prepared commit, before completing
                # its decision. A later --finish must close that log entry as well.
                ready_xids.add(decision.xid)

        for alias in Connections.aliases():
            connection = Atomic.get_connection(alias)
            statements = connection.dialect.two_phase_commit
            if not connection.features.supports_two_phase_commit or statements is None:
                continue
            pattern = DistributedCoordinator._get_participant_gid_like_pattern(alias, statements.like_escape_character)
            _, rows = await connection.execute(
                statements.get_prepared_listing_sql(with_age_limit=bool(older_than_seconds)),
                [pattern, older_than_seconds] if older_than_seconds else [pattern],
            )
            for row in rows:
                xid = row["gid"].removesuffix(f":{alias}")
                if (
                    not DistributedCoordinator._xid_belongs_to_coordinator(xid, coordinator)
                    or xid in xids_with_decisions
                ):
                    continue
                stale.append(
                    StalePreparedTransaction(
                        xid,
                        alias,
                        DistributedTransactionResolution.ROLLBACK,
                        "no decision row found for this xid - presumed abort",
                    )
                )
        return stale, ready_xids
