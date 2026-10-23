from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.tables.table_creation import TableCreation
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_HOSTS_SQL,
    CLICKHOUSE_REPLICATED_ENGINE_PREFIX,
    CLICKHOUSE_UNREPLICATED_LOCAL_TABLES_MESSAGE,
)
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.models import Model


class ClickhouseTableCreation(TableCreation):
    """The ``CREATE TABLE`` of ClickHouse - the primary key, composite too, is the ``PRIMARY KEY``
    clause of the table's engine (``ClickhouseTableOptions``)."""

    __slots__ = ()

    async def raise_if_replicas_keep_rows_apart(self, model: type[Model]) -> None:
        """Refuses a distributed table whose local tables a plain engine keeps on a cluster with several
        replicas of a shard - each replica would hold rows of its own.

        Args:
            model: The model.

        Raises:
            UnSupportedError: The local tables aren't replicated, and a shard has several replicas.
        """
        client = self.editor.client
        cluster = getattr(client, "cluster", None)
        if cluster is None or self.editor.collect_sql:
            return
        options = ClickhouseTableOptions.get_for_model(model, client.dialect)
        if options.distributed_over is None or options.engine.startswith(CLICKHOUSE_REPLICATED_ENGINE_PREFIX):
            return
        rows = await client.execute_dicts(CLICKHOUSE_CLUSTER_HOSTS_SQL, [cluster.name])
        if rows and int(rows[0]["hosts"]) > int(rows[0]["shards"]):
            raise UnSupportedError(
                CLICKHOUSE_UNREPLICATED_LOCAL_TABLES_MESSAGE.format(
                    model=model.__name__, engine=options.engine, cluster=cluster.name
                )
            )

    def get_composite_pk_constraint_sql(self, model: type[Model], field_names: list[str]) -> str:
        return ""
