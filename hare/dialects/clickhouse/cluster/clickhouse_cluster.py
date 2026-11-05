from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from hare.dialects.clickhouse.client.constants import CLICKHOUSE_DEFAULT_DATABASE
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_DATABASE_ENGINES_SQL,
    CLICKHOUSE_REPLICA_SYNC_TEMPLATE,
    CLICKHOUSE_REPLICATED_ENGINE_PREFIX,
    CLICKHOUSE_TABLE_ENGINE_SQL,
)

if TYPE_CHECKING:
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient


class ClickhouseCluster:
    """Connection cluster identity, database-engine snapshot and explicit replica synchronization.

    Args:
        client: The connection.
        name: The cluster's name, as the servers know it.

    """

    __slots__ = ("client", "name", "database_engines")

    def __init__(self, client: ClickhouseClient, name: str) -> None:
        self.client = client
        self.name = name
        #: A connection snapshot, replaced as a whole after schema work. Rendering never performs I/O.
        self.database_engines: MappingProxyType[str, str] | None = None

    async def refresh_databases(self) -> None:
        """Capture engines for all visible databases before compiling schema statements.

        Clear the previous snapshot first: a failed refresh must not allow later
        compilation against catalog facts from before a partially executed DDL.
        """
        self.database_engines = None
        rows = await self.client.execute_dicts(CLICKHOUSE_DATABASE_ENGINES_SQL)
        self.database_engines = MappingProxyType({str(row["name"]): str(row["engine"]) for row in rows})

    @property
    def database(self) -> str:
        """The connection's database - of a table a statement names without one."""
        return self.client.database or CLICKHOUSE_DEFAULT_DATABASE

    async def synchronize_table(self, table_name: str) -> None:
        """Waits until the connection's server holds every row written to a replicated table through
        the other replicas.

        Args:
            table_name: The table.
        """
        rows = await self.client.execute_dicts(CLICKHOUSE_TABLE_ENGINE_SQL, [table_name])
        if rows and str(rows[0]["engine"]).startswith(CLICKHOUSE_REPLICATED_ENGINE_PREFIX):
            await self.client.execute_script(
                CLICKHOUSE_REPLICA_SYNC_TEMPLATE.format(
                    table=self.client.dialect.literals.quote_identifier(table_name)
                )
            )
