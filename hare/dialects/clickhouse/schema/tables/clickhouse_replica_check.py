from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_HOSTS_SQL,
    CLICKHOUSE_UNREPLICATED_LOCAL_TABLES_MESSAGE,
)
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_REPLICA_CHECK_PREVIEW
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


@dataclass(frozen=True, slots=True)
class ClickhouseReplicaCheck:
    """Reject a distributed table whose local engine cannot share rows between replicas.

    The declaration and cluster name are captured during preparation. Only execution
    reads the cluster topology; preview reports that requirement without catalog IO.
    """

    model_name: str
    engine: str
    cluster_name: str
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.READ_CATALOG})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Check the prepared placement before executing CREATE statements."""
        if editor.collect_sql:
            editor.collected_sql.append(CLICKHOUSE_REPLICA_CHECK_PREVIEW.format(cluster=self.cluster_name))
            return
        rows = await editor.client.execute_dicts(CLICKHOUSE_CLUSTER_HOSTS_SQL, [self.cluster_name])
        if rows and int(rows[0]["hosts"]) > int(rows[0]["shards"]):
            raise UnSupportedError(
                CLICKHOUSE_UNREPLICATED_LOCAL_TABLES_MESSAGE.format(
                    model=self.model_name, engine=self.engine, cluster=self.cluster_name
                )
            )
