from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.exceptions import UnSupportedError
from hare.transactions.constants import DISTRIBUTED_DECISIONS_TABLE_NAME
from hare.transactions.distributed.distributed_decision import DistributedDecision

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class DistributedDecisionStore:
    """Durable commit decisions on a selected coordinator connection.

    Recording uses the caller's transaction; reading and completing use a normal coordinator
    client. SQL remains with the dialect. A young decision is returned even when recovery
    cannot act on it yet, so its prepared participants are never mistaken for orphans.
    """

    def __init__(self, client: DatabaseClient) -> None:
        statements = client.dialect.two_phase_commit
        if statements is None:
            raise UnSupportedError(f"The {client.dialect} dialect has no two-phase commit")
        self.client = client
        self.statements = statements
        self.table = client.get_shared_table_name(DISTRIBUTED_DECISIONS_TABLE_NAME)

    async def ensure_schema(self) -> None:
        """Create the decision table through the dialect's idempotent statement."""
        await self.client.execute(self.statements.get_decisions_table_sql(self.table))

    async def record(self, xid: str, coordinator: str, participants: Sequence[str]) -> None:
        """Record the decision within the coordinator transaction, before its COMMIT."""
        await self.ensure_schema()
        await self.client.execute(
            self.statements.get_decision_insert_sql(self.table),
            [xid, coordinator, json.dumps(list(participants))],
        )

    async def complete(self, xid: str) -> None:
        """Mark the decision complete only after every participant has finished."""
        await self.client.execute(self.statements.get_finish_decision_sql(self.table), [xid])

    async def pending(self, older_than_seconds: int) -> tuple[DistributedDecision, ...]:
        """Read all pending decisions, keeping age eligibility separate from existence."""
        await self.ensure_schema()
        _, rows = await self.client.execute(
            self.statements.get_pending_decisions_sql(self.table, with_age_limit=bool(older_than_seconds)),
            [older_than_seconds] if older_than_seconds else None,
        )
        return tuple(
            DistributedDecision(
                row["xid"],
                row["participant_aliases"],
                bool(row["is_stale"]) if older_than_seconds else True,
            )
            for row in rows
        )
