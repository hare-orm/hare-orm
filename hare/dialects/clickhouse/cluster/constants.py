from __future__ import annotations

import re

#: The engine of a database replicating its DDL to its servers by itself.
CLICKHOUSE_REPLICATED_DATABASE_ENGINE = "Replicated"
CLICKHOUSE_DATABASE_ENGINE_SQL = "SELECT engine FROM system.databases WHERE name = currentDatabase()"
#: The tables of the server spreading the rows of a local table over the cluster, with their databases.
CLICKHOUSE_DISTRIBUTED_TABLES_SQL = (
    "SELECT database, name, engine_full FROM system.tables WHERE engine = 'Distributed'"
)
#: The engines keeping a copy of a table's rows on every replica.
CLICKHOUSE_REPLICATED_ENGINE_PREFIX = "Replicated"
CLICKHOUSE_TABLE_ENGINE_SQL = "SELECT engine FROM system.tables WHERE database = currentDatabase() AND name = $1"
#: A replica brought up to the rows written through the other ones.
CLICKHOUSE_REPLICA_SYNC_TEMPLATE = "SYSTEM SYNC REPLICA {table}"
#: The engine of a table spreading the rows of a local one over a cluster, by a key or to any shard.
#: ``{database}`` - ``currentDatabase()`` for a table of the connection's database, the name of another.
CLICKHOUSE_DISTRIBUTED_ENGINE_TEMPLATE = "Distributed({cluster}, {database}, {table}{sharding_key})"
CLICKHOUSE_DISTRIBUTED_TABLE_TEMPLATE = "CREATE {or_replace}TABLE {exists}{table} AS {local_table} ENGINE = {engine};"
#: The engine of the journal of applied migrations on a cluster.
CLICKHOUSE_REPLICATED_JOURNAL_ENGINE = "ReplicatedMergeTree"
#: The servers a cluster runs a statement on.
CLICKHOUSE_CLUSTER_HOSTS_SQL = (
    "SELECT count() AS hosts, uniqExact(shard_num) AS shards FROM system.clusters WHERE cluster = $1"
)
#: A name in backquotes or double quotes - a backslash escapes the next character, a doubled quote is one.
CLICKHOUSE_QUOTED_NAME = r'(?:`(?:[^`\\]|\\.|``)+`|"(?:[^"\\]|\\.|"")+")'
#: The name of a table - quoted or plain, with its database or without.
CLICKHOUSE_STATEMENT_NAME = (
    rf"(?:{CLICKHOUSE_QUOTED_NAME}|[A-Za-z_][A-Za-z0-9_]*)(?:\.(?:{CLICKHOUSE_QUOTED_NAME}|[A-Za-z_][A-Za-z0-9_]*))?"
)
#: The statements a cluster runs on every server, each up to the name ``ON CLUSTER`` follows.
CLICKHOUSE_CLUSTER_STATEMENT_PATTERN = re.compile(
    r"\s*(?P<head>"
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|MATERIALIZED\s+VIEW|DICTIONARY|DATABASE)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"|ALTER\s+TABLE\s+"
    r"|DROP\s+(?:TABLE|VIEW|DICTIONARY|DATABASE)\s+(?:IF\s+EXISTS\s+)?"
    r"|TRUNCATE\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    r"|DELETE\s+FROM\s+"
    rf")(?P<name>{CLICKHOUSE_STATEMENT_NAME})(?P<rest>.*)",
    re.IGNORECASE | re.DOTALL,
)
#: A rename - ``ON CLUSTER`` closes it.
CLICKHOUSE_RENAME_STATEMENT_PATTERN = re.compile(r"\s*RENAME\s+(?:TABLE|DICTIONARY)\s", re.IGNORECASE)
#: A dictionary loaded again - ``ON CLUSTER`` stands before its name.
CLICKHOUSE_DICTIONARY_RELOAD_PATTERN = re.compile(
    r"(\s*SYSTEM\s+RELOAD\s+DICTIONARY\s+)(.*)", re.IGNORECASE | re.DOTALL
)
#: A statement naming its cluster itself.
CLICKHOUSE_ON_CLUSTER_PATTERN = re.compile(r"\sON\s+CLUSTER\s", re.IGNORECASE)
#: The first words of the statements changing rows, not the schema - a mutation, a lightweight DELETE:
#: they run where they are sent, but for the rows of a distributed table.
CLICKHOUSE_ROW_CHANGE_PATTERN = re.compile(r"\s*(?:UPDATE|DELETE)\s", re.IGNORECASE)
#: The changes of a table's columns a distributed table takes as its local one does.
CLICKHOUSE_COLUMN_CHANGE_PATTERN = re.compile(
    r"\s*(?:(?:ADD|DROP|MODIFY|RENAME|COMMENT)\s+COLUMN|MODIFY\s+COMMENT)\s", re.IGNORECASE
)
#: The rewrite of a column's rows a column change ends with (``MODIFY COLUMN ..., UPDATE c = c WHERE 1``) -
#: a distributed table holds no rows and takes no mutation, it takes the column change alone.
CLICKHOUSE_COLUMN_REWRITE_CLAUSE_PATTERN = re.compile(
    rf",\s*UPDATE\s+({CLICKHOUSE_QUOTED_NAME}|\w+)\s*=\s*\1\s+WHERE\s+1\s*;?\s*$", re.IGNORECASE
)
#: The changes of how a column is stored - of the local table alone.
CLICKHOUSE_COLUMN_STORAGE_PATTERN = re.compile(r"\s(?:CODEC\s*\(|TTL\s|REMOVE\s+(?:CODEC|TTL)\b)", re.IGNORECASE)
#: The setting a write into a distributed table waits for its shards by.
CLICKHOUSE_CLUSTER_SESSION_SETTINGS = {"distributed_foreground_insert": 1}
#: A distributed table created by a script - the statements after it are run by it.
CLICKHOUSE_DISTRIBUTED_CREATE_PATTERN = re.compile(
    r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{CLICKHOUSE_STATEMENT_NAME})\s+AS\s+(?P<local>{CLICKHOUSE_STATEMENT_NAME})\s+ENGINE\s*=\s*Distributed\b",
    re.IGNORECASE,
)
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
