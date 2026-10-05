from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from hare.dialects.clickhouse.cluster.clickhouse_cluster_statements import ClickhouseClusterStatements
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_DATABASE_ENGINE_SQL,
    CLICKHOUSE_DISTRIBUTED_CREATE_PATTERN,
    CLICKHOUSE_DISTRIBUTED_TABLES_SQL,
    CLICKHOUSE_REPLICA_SYNC_TEMPLATE,
    CLICKHOUSE_REPLICATED_DATABASE_ENGINE,
    CLICKHOUSE_REPLICATED_ENGINE_PREFIX,
    CLICKHOUSE_TABLE_ENGINE_SQL,
)
from hare.dialects.clickhouse.introspection.constants import CLICKHOUSE_PLAIN_IDENTIFIER_PATTERN
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.clickhouse.client.clickhouse_client import ClickhouseClient


class ClickhouseCluster:
    """The cluster a connection belongs to (its ``cluster`` setting): what the connection's database
    replicates by itself, which of its tables are distributed, and how a statement runs on it.

    Args:
        client: The connection.
        name: The cluster's name, as the servers know it.

    Raises:
        ConfigurationError: The name isn't a cluster's name.
    """

    __slots__ = ("client", "name", "distributed_tables", "replicates_schema")

    def __init__(self, client: ClickhouseClient, name: str) -> None:
        if not CLICKHOUSE_PLAIN_IDENTIFIER_PATTERN.fullmatch(name):
            raise ConfigurationError(f"A ClickHouse connection's cluster must be a cluster's name, got {name!r}")
        self.client = client
        self.name = name
        #: The local table of each distributed table of the database - None until read; read again
        #: after a script, which may create and drop tables.
        self.distributed_tables: dict[str, str] | None = None
        #: Whether the database replicates a change of the schema itself - None until read.
        self.replicates_schema: bool | None = None

    def forget_tables(self) -> None:
        """Has the distributed tables read again by the next statement that needs them."""
        self.distributed_tables = None

    async def fetch_distributed_tables(self) -> dict[str, str]:
        """The distributed tables of the connection's database.

        Returns:
            The name of each -> the name of the local table whose rows it spreads.
        """
        if self.distributed_tables is None:
            rows = await self.client.execute_dicts(CLICKHOUSE_DISTRIBUTED_TABLES_SQL)
            self.distributed_tables = {
                str(row["name"]): ClickhouseTypeNames.get_type_parts(str(row["engine_full"]))[1][2].strip("'")
                for row in rows
            }
        return self.distributed_tables

    async def fetch_replicates_schema(self) -> bool:
        """Whether the connection's database replicates a change of the schema by itself - a database
        of the ``Replicated`` engine, which refuses ``ON CLUSTER``.

        Returns:
            Whether it does.
        """
        if self.replicates_schema is None:
            rows = await self.client.execute_dicts(CLICKHOUSE_DATABASE_ENGINE_SQL)
            self.replicates_schema = bool(rows) and rows[0]["engine"] == CLICKHOUSE_REPLICATED_DATABASE_ENGINE
        return self.replicates_schema

    async def get_statements(self, statements: Sequence[str]) -> list[str]:
        """What the statements of a script run as on the cluster.

        Args:
            statements: The statements.

        Returns:
            The statements - a change of the schema over the cluster, a change of a distributed
            table where it belongs.
        """
        distributed_tables = dict(await self.fetch_distributed_tables())
        replicates_schema = await self.fetch_replicates_schema()
        cluster_statements = []
        for statement in statements:
            cluster_statements.extend(
                ClickhouseClusterStatements.get_statements(
                    statement, self.name, distributed_tables, replicates_schema=replicates_schema
                )
            )
            # A distributed table the script creates takes the statements after it.
            if (created := CLICKHOUSE_DISTRIBUTED_CREATE_PATTERN.match(statement)) is not None:
                distributed_tables[ClickhouseClusterStatements.get_table_name(created.group("name"))] = (
                    ClickhouseClusterStatements.get_table_name(created.group("local"))
                )
        return cluster_statements

    async def get_write_sql(self, statements: Sequence[str]) -> list[str]:
        """What a write of rows runs as on the cluster - the rows of a distributed table are changed
        in its local table on every server.

        Args:
            statements: The write, and the count of its rows that may follow it.

        Returns:
            The statements.
        """
        distributed_tables = await self.fetch_distributed_tables()
        if not distributed_tables:
            return list(statements)
        (write_sql,) = ClickhouseClusterStatements.get_statements(
            statements[0], self.name, distributed_tables, replicates_schema=True
        )
        return [write_sql, *statements[1:]]

    def get_database_statement(self, statement: str) -> str:
        """A statement creating or dropping the connection's database, on every server.

        Args:
            statement: ``CREATE DATABASE ...`` or ``DROP DATABASE ...``.

        Returns:
            The statement over the cluster.
        """
        return ClickhouseClusterStatements.get_statements(statement, self.name, {})[0]

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
