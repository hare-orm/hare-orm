from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_observed_table_options import ClickhouseObservedTableOptions
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_EXPRESSION_COMPARISON_SQL,
    CLICKHOUSE_FORMATTED_QUERY_SQL,
    CLICKHOUSE_PROJECTION_COMPARISON_SQL,
    CLICKHOUSE_TTL_COMPARISON_SQL,
)
from hare.dialects.clickhouse.schema.constants import (
    CLICKHOUSE_PROJECTION_REBUILD_MODE,
    CLICKHOUSE_PROJECTION_REBUILD_SETTINGS,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.inspectdb.introspection.table_info import TableInfo


class ClickhouseDeclaredTableOptions:
    """The options of a ClickHouse table as its model declares them, wherever both mean the same
    storage: the server writes an expression its own way (``INTERVAL 30 DAY`` as
    ``toIntervalDay(30)``), gives a codec the arguments a declaration leaves out, and sorts a table
    declaring no sort by its primary key."""

    @classmethod
    async def fetch(
        cls,
        connection: DatabaseClient,
        observed: ClickhouseTableOptions | None,
        declared: ClickhouseTableOptions | None,
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
    ) -> ClickhouseTableOptions | None:
        """Returns a table's options with each declared option kept where the table's own means the
        same.

        Args:
            connection: The connection the table was read on.
            observed: The options read from the table, naming columns.
            declared: The model's options, if it declares any.
            table_info: The table.
            column_to_field_name: Column name -> the name of the model field owning it.

        Returns:
            The options, naming fields; None for a table of default options no model declares any for.
        """
        if observed is not None:
            observed = observed.with_field_names(column_to_field_name)
        if declared is None:
            return observed
        if observed is None:
            observed = ClickhouseTableOptions()
        field_to_column_name = {field_name: column_name for column_name, field_name in column_to_field_name.items()}
        quote_identifier = connection.dialect.literals.quote_identifier

        def get_key_sql(key: str | RawSQLTerm) -> str:
            return key.sql if isinstance(key, RawSQLTerm) else quote_identifier(field_to_column_name.get(key, key))

        primary_key = tuple(
            column_to_field_name.get(column.name, column.name)
            for column in sorted(
                (column for column in table_info.columns if column.is_pk), key=lambda column: column.pk_position or 0
            )
        )
        expression_sql = CLICKHOUSE_EXPRESSION_COMPARISON_SQL
        # The statements each option is compared by, in pairs - the declared one and the table's; None
        # for an option that differs whatever the server makes of its text.
        compared_statements: dict[str, list[tuple[str, str]] | None] = {
            "engine": [
                (
                    expression_sql.format(sql=declared.engine.strip().removesuffix("()")),
                    expression_sql.format(sql=observed.engine.strip().removesuffix("()")),
                )
            ],
            "order_by": cls.get_compared_lists(
                [get_key_sql(key) for key in declared.order_by or primary_key],
                [get_key_sql(key) for key in observed.order_by or primary_key],
                expression_sql,
            ),
            "sample_by": cls.get_compared_values(
                {"": get_key_sql(declared.sample_by)} if declared.sample_by is not None else {},
                {"": get_key_sql(observed.sample_by)} if observed.sample_by is not None else {},
                expression_sql,
            ),
            "partition_by": cls.get_compared_values(
                {"": declared.partition_by.sql} if declared.partition_by is not None else {},
                {"": observed.partition_by.sql} if observed.partition_by is not None else {},
                expression_sql,
            ),
            "ttl": cls.get_compared_values(
                {"": declared.ttl.sql} if declared.ttl is not None else {},
                {"": observed.ttl.sql} if observed.ttl is not None else {},
                CLICKHOUSE_TTL_COMPARISON_SQL,
            ),
            "column_ttls": cls.get_compared_values(
                {name: ttl.sql for name, ttl in declared.column_ttls},
                {name: ttl.sql for name, ttl in observed.column_ttls},
                expression_sql,
            ),
            "projections": cls.get_compared_values(
                {projection.name: projection.query.sql for projection in declared.projections},
                {projection.name: projection.query.sql for projection in observed.projections},
                CLICKHOUSE_PROJECTION_COMPARISON_SQL,
            ),
            "column_codecs": [] if cls.codecs_are_same(declared, observed) else None,
            "settings": [] if await cls.settings_are_same(connection, declared, observed) else None,
        }
        formatted_statements = await cls.fetch_formatted_statements(
            connection,
            sorted(
                {
                    statement
                    for statement_pairs in compared_statements.values()
                    for statement_pair in statement_pairs or ()
                    if statement_pair[0] != statement_pair[1]
                    for statement in statement_pair
                }
            ),
        )
        return dataclasses.replace(
            observed,
            **{
                option_name: getattr(declared, option_name)
                for option_name, statement_pairs in compared_statements.items()
                if statement_pairs is not None
                and all(
                    declared_statement == observed_statement
                    or (
                        formatted_statements.get(declared_statement) is not None
                        and formatted_statements.get(declared_statement)
                        == formatted_statements.get(observed_statement)
                    )
                    for declared_statement, observed_statement in statement_pairs
                )
            },
        )

    @staticmethod
    def get_compared_lists(
        declared_sqls: list[str], observed_sqls: list[str], statement_sql: str
    ) -> list[tuple[str, str]] | None:
        """The statements two lists of expressions are compared by, one of the first with the same one
        of the second.

        Args:
            declared_sqls: The declared expressions.
            observed_sqls: The table's.
            statement_sql: The statement an expression is compared in.

        Returns:
            The pairs of statements; None for lists of other lengths.
        """
        if len(declared_sqls) != len(observed_sqls):
            return None
        return [
            (statement_sql.format(sql=declared_sql), statement_sql.format(sql=observed_sql))
            for declared_sql, observed_sql in zip(declared_sqls, observed_sqls, strict=True)
        ]

    @staticmethod
    def get_compared_values(
        declared_sqls: dict[str, str], observed_sqls: dict[str, str], statement_sql: str
    ) -> list[tuple[str, str]] | None:
        """The statements the named expressions of two declarations are compared by.

        Args:
            declared_sqls: The declared expressions, by name.
            observed_sqls: The table's.
            statement_sql: The statement an expression is compared in.

        Returns:
            The pairs of statements; None when the two name other things.
        """
        if declared_sqls.keys() != observed_sqls.keys():
            return None
        return [
            (statement_sql.format(sql=declared_sql), statement_sql.format(sql=observed_sqls[name]))
            for name, declared_sql in declared_sqls.items()
        ]

    @staticmethod
    async def fetch_formatted_statements(connection: DatabaseClient, statements: list[str]) -> dict[str, str | None]:
        """Statements as the server writes them.

        Args:
            connection: The connection.
            statements: The statements.

        Returns:
            Each statement's text of the server by its own; None for one the server can't read.
        """
        if not statements:
            return {}
        columns_sql = ", ".join(
            f"{CLICKHOUSE_FORMATTED_QUERY_SQL.format(parameter=f'${position}')} AS statement_{position}"
            for position in range(1, len(statements) + 1)
        )
        (row,) = await connection.execute_dicts(f"SELECT {columns_sql}", statements)
        return {statement: row[f"statement_{position}"] for position, statement in enumerate(statements, 1)}

    @staticmethod
    def codecs_are_same(declared: ClickhouseTableOptions, observed: ClickhouseTableOptions) -> bool:
        """Whether the columns of a table are compressed as declared - a codec declared without its
        arguments is any one of its name: the server adds the arguments it chose.

        Args:
            declared: The model's options.
            observed: The table's.

        Returns:
            Whether both name the same codecs of the same columns.
        """
        declared_codecs, observed_codecs = dict(declared.column_codecs), dict(observed.column_codecs)
        if declared_codecs.keys() != observed_codecs.keys():
            return False
        for field_name, declared_sql in declared_codecs.items():
            declared_parts = ClickhouseSqlParts.split(declared_sql)
            observed_parts = ClickhouseSqlParts.split(observed_codecs[field_name])
            if len(declared_parts) != len(observed_parts):
                return False
            for declared_codec, observed_codec in zip(declared_parts, observed_parts, strict=True):
                if "(" not in declared_codec:
                    observed_codec = observed_codec.partition("(")[0]
                if "".join(declared_codec.split()) != "".join(observed_codec.split()):
                    return False
        return True

    @staticmethod
    async def settings_are_same(
        connection: DatabaseClient, declared: ClickhouseTableOptions, observed: ClickhouseTableOptions
    ) -> bool:
        """Whether a table has the declared settings - a declared one the table doesn't show holds the
        server's own value, or is one a table with projections gets anyway.

        Args:
            connection: The connection.
            declared: The model's options.
            observed: The table's - without the settings of the server's own values.

        Returns:
            Whether both set the same.
        """
        declared_settings, observed_settings = dict(declared.settings), dict(observed.settings)
        if any(
            name not in declared_settings or str(declared_settings[name]) != str(value)
            for name, value in observed_settings.items()
        ):
            return False
        unseen_settings = {
            name: value
            for name, value in declared_settings.items()
            if name not in observed_settings
            and not (
                observed.projections
                and name in CLICKHOUSE_PROJECTION_REBUILD_SETTINGS
                and value == CLICKHOUSE_PROJECTION_REBUILD_MODE
            )
        }
        if not unseen_settings:
            return True
        server_values = await ClickhouseObservedTableOptions.fetch_server_setting_values(
            connection, list(unseen_settings)
        )
        return all(server_values.get(name) == str(value) for name, value in unseen_settings.items())
