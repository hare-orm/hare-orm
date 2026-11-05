from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.cluster.messages import DISTRIBUTED_MUTATION_WITHOUT_CLUSTER
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.sql.builder.tables.table import Table
    from hare.sql.sql_context import SqlContext


@dataclass(frozen=True, slots=True)
class ClickhousePlacementPlan:
    """Targets selected from a declaration before rendering a statement.

    Reads and inserts use the logical table. Mutations of a distributed table use
    its storage table on all shards, independently of database schema replication.
    Schema statements use ``schema_cluster``; column changes can address both
    tables, while storage properties belong only to the storage table.
    """

    logical_table: str
    storage_table: str
    database: str | None
    cluster: str | None
    replicates_schema: bool = False

    @classmethod
    def for_table(
        cls, table: Table, sql_context: SqlContext, *, replicates_schema: bool = False
    ) -> ClickhousePlacementPlan:
        options = ClickhouseTableOptions.get_of_dialect(table.table_options, sql_context.dialect)
        logical_table = table.get_name_without_alias()
        return cls(
            logical_table,
            options.distributed_over or logical_table,
            table.get_schema_name(),
            getattr(sql_context, "cluster_name", None),
            replicates_schema,
        )

    @property
    def schema_cluster(self) -> str | None:
        return None if self.replicates_schema else self.cluster

    @staticmethod
    def get_cluster_sql(cluster: str | None, sql_context: SqlContext) -> str:
        """Quote an explicitly selected cluster; an absent target adds no clause."""
        return "" if cluster is None else f" ON CLUSTER {sql_context.quote(cluster)}"

    def get_mutation_target_sql(self, sql_context: SqlContext) -> str:
        """Render the storage identity and explicit mutation placement without reading SQL."""
        table_sql = sql_context.quote(self.storage_table)
        if self.database is not None:
            table_sql = f"{sql_context.quote(self.database)}.{table_sql}"
        if self.storage_table != self.logical_table:
            if self.cluster is None:
                raise ConfigurationError(DISTRIBUTED_MUTATION_WITHOUT_CLUSTER)
            table_sql += self.get_cluster_sql(self.cluster, sql_context)
        return table_sql
