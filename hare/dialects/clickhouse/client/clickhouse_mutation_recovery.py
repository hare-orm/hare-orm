from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.core.resource_completion import ResourceCompletion
from hare.dialects.clickhouse.client.constants import (
    CLICKHOUSE_FAILED_MUTATIONS_PATTERN,
    CLICKHOUSE_KILL_MUTATIONS_TEMPLATE,
)
from hare.dialects.clickhouse.cluster.clickhouse_placement_plan import ClickhousePlacementPlan

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient


@dataclass(frozen=True, slots=True)
class ClickhouseMutationRecovery:
    """Cancel only failed mutation IDs on the target selected during compilation.

    A server error identifies mutation IDs within one physical table, not across
    shards. Only local operations receive this handler. Distributed failures need
    server-specific inspection; broadcasting their IDs could cancel unrelated work.
    No SQL text is parsed and no cluster is added after failure.
    Recovery is best effort: the caller receives the original write error.
    """

    database: str
    table: str

    @classmethod
    def for_mutation(
        cls, placement: ClickhousePlacementPlan, default_database: str
    ) -> ClickhouseMutationRecovery | None:
        """Capture a local target; shard-local IDs cannot identify distributed mutations."""
        if placement.storage_table != placement.logical_table:
            return None
        return cls(
            placement.database or default_database,
            placement.storage_table,
        )

    async def recover(self, client: DatabaseClient, error: BaseException) -> None:
        connection: ClickhouseClient = client  # type: ignore[assignment]
        if connection.is_transaction_client or not isinstance(error, connection.driver_errors.driver):
            return
        match = CLICKHOUSE_FAILED_MUTATIONS_PATTERN.search(str(error))
        if match is None:
            return
        literals = connection.dialect.literals
        sql = CLICKHOUSE_KILL_MUTATIONS_TEMPLATE.format(
            database=literals.get_string_literal_sql(self.database),
            table=literals.get_string_literal_sql(self.table),
            ids=", ".join(literals.get_string_literal_sql(value.strip()) for value in match.group(1).split(",")),
        )
        try:
            await ResourceCompletion.wait(connection.execute_script(sql))
        except Exception:
            # A missing mutation or a failed cleanup must not replace the original driver error.
            return
