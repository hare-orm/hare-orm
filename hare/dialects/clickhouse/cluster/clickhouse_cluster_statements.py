from __future__ import annotations

from collections.abc import Mapping

from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_STATEMENT_PATTERN,
    CLICKHOUSE_COLUMN_CHANGE_PATTERN,
    CLICKHOUSE_COLUMN_STORAGE_PATTERN,
    CLICKHOUSE_DICTIONARY_RELOAD_PATTERN,
    CLICKHOUSE_ON_CLUSTER_PATTERN,
    CLICKHOUSE_RENAME_STATEMENT_PATTERN,
    CLICKHOUSE_ROW_CHANGE_PATTERN,
)


class ClickhouseClusterStatements:
    """The statements of a connection to a cluster: a change of the schema runs on every server
    (``ON CLUSTER``), and a change of a distributed table goes where it belongs - a change of rows and of
    storage to the local table on every server, a change of columns to both tables."""

    @classmethod
    def get_statements(
        cls,
        statement: str,
        cluster: str,
        distributed_tables: Mapping[str, str],
        *,
        replicates_schema: bool = False,
    ) -> list[str]:
        """Returns what a statement runs as on a cluster.

        Args:
            statement: One statement.
            cluster: The cluster's name.
            distributed_tables: The name of each distributed table of the database -> the name of the
                local table whose rows it spreads.
            replicates_schema: Whether the database replicates a change of the schema itself - then
                nothing names the cluster but a change of a distributed table's rows.

        Returns:
            The statements - the statement itself when it is none a cluster runs another way.
        """
        if CLICKHOUSE_ON_CLUSTER_PATTERN.search(statement):
            return [statement]
        on_cluster_sql = "" if replicates_schema else f" ON CLUSTER {cluster}"
        if CLICKHOUSE_RENAME_STATEMENT_PATTERN.match(statement):
            return [f"{statement.rstrip().rstrip(';')}{on_cluster_sql}"]
        if (reload_match := CLICKHOUSE_DICTIONARY_RELOAD_PATTERN.fullmatch(statement)) is not None:
            cluster_sql = f"{on_cluster_sql.lstrip()} " if on_cluster_sql else ""
            return [f"{reload_match.group(1)}{cluster_sql}{reload_match.group(2)}"]
        match = CLICKHOUSE_CLUSTER_STATEMENT_PATTERN.fullmatch(statement)
        if match is None:
            return [statement]
        head, name, rest = match.group("head"), match.group("name"), match.group("rest")
        verb = head.split()[0].upper()
        local_table = distributed_tables.get(cls.get_table_name(name))
        changes_rows = verb == "DELETE" or (verb == "ALTER" and CLICKHOUSE_ROW_CHANGE_PATTERN.match(rest) is not None)
        if local_table is None:
            # The rows of a table are changed where the statement is sent: a replicated table copies
            # the change to its replicas itself.
            return [statement if changes_rows else f"{head}{name}{on_cluster_sql}{rest}"]
        local_name = cls.get_local_name(name, local_table)
        if changes_rows:
            # Over the cluster whatever the database replicates: each shard holds rows of its own.
            return [f"{head}{local_name} ON CLUSTER {cluster}{rest}"]
        local_statement = f"{head}{local_name}{on_cluster_sql}{rest}"
        distributed_statement = f"{head}{name}{on_cluster_sql}{rest}"
        if verb == "ALTER" and cls.changes_columns(rest):
            return [local_statement, distributed_statement]
        # The distributed table itself is created and dropped; any other change is of the storage.
        return [distributed_statement] if verb in {"CREATE", "DROP"} else [local_statement]

    @staticmethod
    def changes_columns(alter_sql: str) -> bool:
        """Whether an ``ALTER TABLE`` changes what columns a table has - not how one is stored.

        Args:
            alter_sql: The statement after its table's name.

        Returns:
            Whether a distributed table takes the change with its local one.
        """
        return (
            CLICKHOUSE_COLUMN_CHANGE_PATTERN.match(alter_sql) is not None
            and CLICKHOUSE_COLUMN_STORAGE_PATTERN.search(alter_sql) is None
        )

    @staticmethod
    def get_table_name(name_sql: str) -> str:
        """A table's own name of how a statement names it.

        Args:
            name_sql: The name - quoted or plain, with its database or without.

        Returns:
            The name without its quotes and its database.
        """
        table_sql = name_sql
        for quote in ('"', "`"):
            if name_sql.endswith(quote):
                table_sql = name_sql[name_sql.rindex(quote, 0, -1) :]
                break
        else:
            table_sql = name_sql.rpartition(".")[2]
        return table_sql.strip('"`')

    @classmethod
    def get_local_name(cls, name_sql: str, local_table: str) -> str:
        """The local table of a distributed one, named as a statement names the distributed one.

        Args:
            name_sql: The distributed table as the statement names it.
            local_table: The local table's name.

        Returns:
            The local table - quoted, in the same database.
        """
        table_name = cls.get_table_name(name_sql)
        database_sql = name_sql[: len(name_sql) - len(table_name) - (2 if name_sql[-1] in '"`' else 0)]
        return f'{database_sql}"{local_table}"'
