from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_CODEC_PREFIX,
    CLICKHOUSE_ENGINE_CLAUSES,
    CLICKHOUSE_ESCAPED_CHARACTER_PATTERN,
    CLICKHOUSE_INTEGER_SETTING_PATTERN,
    CLICKHOUSE_PROJECTION_DEFINITION_PREFIX,
    CLICKHOUSE_SETTINGS_CLAUSE,
    CLICKHOUSE_TABLE_SETTING_DEFAULTS_SQL,
    CLICKHOUSE_TTL_CLAUSE,
)
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTING_VALUE,
    CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTINGS,
    CLICKHOUSE_PROJECTION_REBUILD_MODE,
    CLICKHOUSE_PROJECTION_REBUILD_SETTINGS,
)
from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ClickhouseObservedTableOptions:
    """The options a ClickHouse table has - its engine, keys, time to live and settings, the
    compression and the time to live of its columns, its projections - read from what the server
    reports of the table."""

    @classmethod
    async def fetch(
        cls,
        connection: DatabaseClient,
        table_row: dict[str, Any],
        column_rows: list[dict[str, Any]],
        primary_key_columns: list[str],
    ) -> ClickhouseTableOptions | None:
        """Builds a table's options.

        Args:
            connection: The connection.
            table_row: The table's row of ``system.tables``.
            column_rows: Its rows of ``system.columns``.
            primary_key_columns: The columns of its primary key, in the key's order.

        Returns:
            The options naming columns, None when every one has its default.
        """
        column_names = {str(row["name"]) for row in column_rows}
        clauses = ClickhouseSqlParts.get_clauses(str(table_row["engine_full"]), CLICKHOUSE_ENGINE_CLAUSES)
        order_by = tuple(
            cls.get_key(key_sql, column_names) for key_sql in ClickhouseSqlParts.split(str(table_row["sorting_key"]))
        )
        sampling_key_sql = str(table_row["sampling_key"]).strip()
        partition_key_sql = str(table_row["partition_key"]).strip()
        column_ttls, projections = cls.get_column_list_options(str(table_row["create_table_query"]), column_names)
        table_settings = cls.get_settings(clauses.get(CLICKHOUSE_SETTINGS_CLAUSE, ""))
        lightweight_updates = all(
            table_settings.get(name) == CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTING_VALUE
            for name in CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTINGS
        )
        if lightweight_updates:
            for name in CLICKHOUSE_LIGHTWEIGHT_UPDATE_SETTINGS:
                del table_settings[name]
        settings = await cls.fetch_changed_settings(
            connection,
            table_settings,
            has_projections=bool(projections),
        )
        return ClickhouseTableOptions.from_observed(
            {
                "engine": clauses[""],
                # A sample is picked by a key of the sort the options name.
                "order_by": () if list(order_by) == primary_key_columns and not sampling_key_sql else order_by,
                "sample_by": cls.get_key(sampling_key_sql, column_names) if sampling_key_sql else None,
                "partition_by": RawSQLTerm(partition_key_sql) if partition_key_sql else None,
                "ttl": RawSQLTerm(clauses[CLICKHOUSE_TTL_CLAUSE]) if clauses.get(CLICKHOUSE_TTL_CLAUSE) else None,
                "settings": tuple(settings.items()),
                "column_codecs": tuple(
                    (str(row["name"]), str(row["compression_codec"])[len(CLICKHOUSE_CODEC_PREFIX) : -1])
                    for row in column_rows
                    if str(row["compression_codec"]).startswith(CLICKHOUSE_CODEC_PREFIX)
                ),
                "column_ttls": tuple(column_ttls),
                "projections": tuple(projections),
                "lightweight_updates": lightweight_updates,
            }
        )

    @staticmethod
    def get_key(key_sql: str, column_names: set[str]) -> str | RawSQLTerm:
        """A key of a table as its options name it.

        Args:
            key_sql: The key, as the server writes it.
            column_names: The table's columns.

        Returns:
            The column the key is, or the expression.
        """
        identifier = ClickhouseSqlParts.get_identifier(key_sql)
        return identifier if identifier in column_names else RawSQLTerm(key_sql)

    @staticmethod
    def get_settings(settings_sql: str) -> dict[str, str | int]:
        """The settings of a table.

        Args:
            settings_sql: ``index_granularity = 8192, storage_policy = 'default'``.

        Returns:
            The value of each setting by its name - a whole number, or text.
        """
        settings: dict[str, str | int] = {}
        for setting_sql in ClickhouseSqlParts.split(settings_sql):
            name, _, value_sql = setting_sql.partition("=")
            value_sql = value_sql.strip()
            if value_sql.startswith("'"):
                settings[name.strip()] = CLICKHOUSE_ESCAPED_CHARACTER_PATTERN.sub(r"\1", value_sql[1:-1])
            elif CLICKHOUSE_INTEGER_SETTING_PATTERN.fullmatch(value_sql):
                settings[name.strip()] = int(value_sql)
            else:
                settings[name.strip()] = value_sql
        return settings

    @staticmethod
    async def fetch_server_setting_values(connection: DatabaseClient, names: list[str]) -> dict[str, str]:
        """The server's own values of table settings.

        Args:
            connection: The connection.
            names: The settings.

        Returns:
            The value of each setting the server knows, by its name.
        """
        names_sql = ", ".join(f"'{name}'" for name in names if name.isidentifier())
        if not names_sql:
            return {}
        rows = await connection.execute_dicts(CLICKHOUSE_TABLE_SETTING_DEFAULTS_SQL.format(names=names_sql))
        return {str(row["name"]): str(row["value"]) for row in rows}

    @classmethod
    async def fetch_changed_settings(
        cls, connection: DatabaseClient, settings: dict[str, str | int], *, has_projections: bool
    ) -> dict[str, str | int]:
        """The settings of a table a declaration of it names: not the ones holding the server's own
        value (the server writes ``index_granularity`` into every table), not the ones a table with
        projections gets for them.

        Args:
            connection: The connection.
            settings: The table's settings.
            has_projections: Whether the table has projections.

        Returns:
            The settings.
        """
        server_values = await cls.fetch_server_setting_values(connection, list(settings))
        return {
            name: value
            for name, value in settings.items()
            if server_values.get(name) != str(value)
            and not (
                has_projections
                and name in CLICKHOUSE_PROJECTION_REBUILD_SETTINGS
                and value == CLICKHOUSE_PROJECTION_REBUILD_MODE
            )
        }

    @staticmethod
    def get_column_list_options(
        create_table_sql: str, column_names: set[str]
    ) -> tuple[list[tuple[str, RawSQLTerm]], list[ClickhouseProjection]]:
        """What the column list of a table's ``CREATE TABLE`` alone tells - the time to live of its
        columns and its projections.

        Args:
            create_table_sql: The statement, as the server keeps it.
            column_names: The table's columns.

        Returns:
            The ``(column, expression)`` of each column with a time to live, and the projections.
        """
        column_ttls = []
        projections = []
        column_list_sql, _ = ClickhouseSqlParts.get_parenthesised(create_table_sql)
        for definition_sql in ClickhouseSqlParts.split(column_list_sql):
            if definition_sql.startswith(CLICKHOUSE_PROJECTION_DEFINITION_PREFIX):
                name, query_sql = ClickhouseSqlParts.get_leading_identifier(
                    definition_sql[len(CLICKHOUSE_PROJECTION_DEFINITION_PREFIX) :].lstrip()
                )
                projections.append(
                    ClickhouseProjection(name, RawSQLTerm(ClickhouseSqlParts.get_parenthesised(query_sql)[0].strip()))
                )
                continue
            # The server quotes the name of every column - an index or a constraint begins with a word.
            if not definition_sql.startswith("`"):
                continue
            name, column_sql = ClickhouseSqlParts.get_leading_identifier(definition_sql)
            ttl_index = ClickhouseSqlParts.find_words(column_sql, CLICKHOUSE_TTL_CLAUSE)
            if name not in column_names or ttl_index < 0:
                continue
            ttl_sql = column_sql[ttl_index + len(CLICKHOUSE_TTL_CLAUSE) :]
            settings_index = ClickhouseSqlParts.find_words(ttl_sql, CLICKHOUSE_SETTINGS_CLAUSE)
            if settings_index >= 0:
                ttl_sql = ttl_sql[:settings_index]
            column_ttls.append((name, RawSQLTerm(ttl_sql.strip())))
        return column_ttls, projections
