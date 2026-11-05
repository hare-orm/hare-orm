from __future__ import annotations

CLICKHOUSE_DATABASE_CREATE_TEMPLATE = "CREATE DATABASE {if_not_exists}{database}{on_cluster}{engine}"
CLICKHOUSE_DATABASE_DROP_TEMPLATE = "DROP DATABASE {if_exists}{database}{on_cluster}{sync}"

#: Database engines that propagate their own metadata changes.
CLICKHOUSE_REPLICATED_DATABASE_ENGINE = "Replicated"
CLICKHOUSE_SCHEMA_REPLICATING_DATABASE_ENGINES = frozenset({CLICKHOUSE_REPLICATED_DATABASE_ENGINE, "Shared"})
CLICKHOUSE_DATABASE_ENGINES_SQL = "SELECT name, engine FROM system.databases"
CLICKHOUSE_DATABASE_ENGINE_SQL = "SELECT engine FROM system.databases WHERE name = $1"
#: The engines keeping a copy of a table's rows on every replica.
CLICKHOUSE_REPLICATED_ENGINE_PREFIX = "Replicated"
CLICKHOUSE_TABLE_ENGINE_SQL = "SELECT engine FROM system.tables WHERE database = currentDatabase() AND name = $1"
#: A replica brought up to the rows written through the other ones.
CLICKHOUSE_REPLICA_SYNC_TEMPLATE = "SYSTEM SYNC REPLICA {table}"
CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE = "CREATE {or_replace}TABLE {exists}{table} AS {local_table} ENGINE = {engine};"
#: The engine of the journal of applied migrations on a cluster.
CLICKHOUSE_REPLICATED_JOURNAL_ENGINE = "ReplicatedMergeTree"
#: The servers a cluster runs a statement on.
CLICKHOUSE_CLUSTER_HOSTS_SQL = (
    "SELECT count() AS hosts, uniqExact(shard_num) AS shards FROM system.clusters WHERE cluster = $1"
)
#: The setting a write into a distributed table waits for its shards by.
CLICKHOUSE_CLUSTER_SESSION_SETTINGS = {"distributed_foreground_insert": 1}
#: The macro a replica path names its shard by - a replicated table's replicas are then those of one shard.
CLICKHOUSE_SHARD_MACRO = "{shard}"
#: The macro a replica path names its table's own UUID by - the one a remade table's path differs by.
CLICKHOUSE_UUID_MACRO = "{uuid}"
#: The refusal of a distributed table whose local tables a plain engine keeps on a cluster with several
#: replicas of a shard - a written row lands on one replica, and a read may ask another.
CLICKHOUSE_UNREPLICATED_LOCAL_TABLES_MESSAGE = (
    "{model}: the local tables of the distributed table are kept by {engine} on the cluster {cluster!r}, whose "
    "shards have several replicas - a row written lands on one replica, and a read may ask another. Store "
    'them by a Replicated engine (ClickhouseTableOptions(engine="ReplicatedMergeTree", ...))'
)
