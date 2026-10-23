from __future__ import annotations

from collections.abc import Mapping

from hare.dialects.clickhouse.cluster.constants import (
    CLICKHOUSE_CLUSTER_STATEMENT_PATTERN,
    CLICKHOUSE_COLUMN_CHANGE_PATTERN,
    CLICKHOUSE_COLUMN_REWRITE_CLAUSE_PATTERN,
    CLICKHOUSE_COLUMN_STORAGE_PATTERN,
    CLICKHOUSE_DICTIONARY_RELOAD_PATTERN,
    CLICKHOUSE_ON_CLUSTER_PATTERN,
    CLICKHOUSE_RENAME_STATEMENT_PATTERN,
    CLICKHOUSE_ROW_CHANGE_PATTERN,
)
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts


class ClickhouseClusterStatements:
    """The statements of a connection to a cluster: a change of the schema runs on every server
    (``ON CLUSTER``), and a change of a distributed table goes where it belongs - a change of rows and of
    storage to the local table on every server, a change of columns to both tables."""

    @classmethod
    def get_statements(
        cls,
        statement: str,
        cluster: str,
        distributed_tables: Mapping[tuple[str, str], str],
        *,
        database: str,
        replicates_schema: bool = False,
    ) -> list[str]:
        """Returns what a statement runs as on a cluster.

        Args:
            statement: One statement.
            cluster: The cluster's name.
            distributed_tables: The database and name of each distributed table -> the name of the
                local table whose rows it spreads, in the same database.
            database: The connection's database - of a table the statement names without one.
            replicates_schema: Whether the database replicates a change of the schema itself - then
                nothing names the cluster but a change of a distributed table's rows.

        Returns:
            The statements - the statement itself when it is none a cluster runs another way.
        """
        if ClickhouseSqlParts.search_outside_strings(CLICKHOUSE_ON_CLUSTER_PATTERN, statement):
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
        table_database, table_name = cls.get_table_key(name)
        local_table = distributed_tables.get((table_database or database, table_name))
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
            # The local table's rows are rewritten with the change - the distributed table has none.
            distributed_rest = CLICKHOUSE_COLUMN_REWRITE_CLAUSE_PATTERN.sub("", rest)
            return [local_statement, f"{head}{name}{on_cluster_sql}{distributed_rest}"]
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
            and ClickhouseSqlParts.search_outside_strings(CLICKHOUSE_COLUMN_STORAGE_PATTERN, alter_sql) is None
        )

    @staticmethod
    def get_table_key(name_sql: str) -> tuple[str | None, str]:
        """The database and the own name of a table, as a statement names it.

        Args:
            name_sql: The name - quoted or plain, with its database or without.

        Returns:
            The database without its quotes - None for a name without one - and the table's own name.
        """
        name, rest = ClickhouseSqlParts.get_leading_identifier(name_sql)
        if not rest.startswith("."):
            return None, name
        table_name, _ = ClickhouseSqlParts.get_leading_identifier(rest[1:])
        return name, table_name

    @staticmethod
    def get_name_parts(name_sql: str) -> tuple[str, str]:
        """Splits how a statement names a table into its database and its own name.

        Args:
            name_sql: The name - quoted or plain, with its database or without.

        Returns:
            The database with the dot after it, as written - empty without one - and the table's own name
            without its quotes.
        """
        name, rest = ClickhouseSqlParts.get_leading_identifier(name_sql)
        if not rest.startswith("."):
            return "", name
        table_name, _ = ClickhouseSqlParts.get_leading_identifier(rest[1:])
        return name_sql[: len(name_sql) - len(rest) + 1], table_name

    @classmethod
    def get_table_name(cls, name_sql: str) -> str:
        """A table's own name of how a statement names it.

        Args:
            name_sql: The name - quoted or plain, with its database or without.

        Returns:
            The name without its quotes and its database.
        """
        return cls.get_table_key(name_sql)[1]

    @classmethod
    def get_local_name(cls, name_sql: str, local_table: str) -> str:
        """The local table of a distributed one, named as a statement names the distributed one.

        Args:
            name_sql: The distributed table as the statement names it.
            local_table: The local table's name.

        Returns:
            The local table - quoted, in the same database.
        """
        database_sql, _ = cls.get_name_parts(name_sql)
        # A backslash escapes the next character of a quoted name, a doubled quote is one.
        quoted_table = local_table.replace("\\", "\\\\").replace('"', '""')
        return f'{database_sql}"{quoted_table}"'
