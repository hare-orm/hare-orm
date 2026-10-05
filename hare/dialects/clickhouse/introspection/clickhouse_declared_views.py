from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view import View
from hare.dialects.clickhouse.introspection.clickhouse_sql_parts import ClickhouseSqlParts
from hare.dialects.clickhouse.introspection.constants import (
    CLICKHOUSE_APPEND_CLAUSE,
    CLICKHOUSE_DEPENDS_ON_CLAUSE,
    CLICKHOUSE_ENGINE_CLAUSE,
    CLICKHOUSE_ENGINE_CLAUSES,
    CLICKHOUSE_MATERIALIZED_VIEW_CLAUSES,
    CLICKHOUSE_MATERIALIZED_VIEW_ENGINE,
    CLICKHOUSE_NO_SORT_SQL,
    CLICKHOUSE_ORDER_BY_CLAUSE,
    CLICKHOUSE_PARTITION_BY_CLAUSE,
    CLICKHOUSE_QUERY_TREE_SQL,
    CLICKHOUSE_REFRESH_CLAUSE,
    CLICKHOUSE_SCHEDULE_PLURAL_PATTERN,
    CLICKHOUSE_TO_CLAUSE,
    CLICKHOUSE_VIEW_ENGINE,
    CLICKHOUSE_VIEW_QUERY_WORD,
    CLICKHOUSE_VIEWS_SQL,
)
from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView
from hare.exceptions import DatabaseError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ClickhouseDeclaredViews:
    """The views a model declares as ClickHouse has them: a view the database lacks is left out, one
    defined another way comes as the database defines it, one meaning the same stays as declared - the
    server keeps a query in its own words, each table of it named with its database."""

    @classmethod
    async def fetch(
        cls,
        connection: DatabaseClient,
        schema: str,
        views: Sequence[View],
        materialized_views: Sequence[MaterializedView],
    ) -> tuple[tuple[View, ...], tuple[MaterializedView, ...]]:
        """Returns the views of a model as the database has them.

        Args:
            connection: The connection.
            schema: The database of the model.
            views: The views the model declares.
            materialized_views: Its materialized views.

        Returns:
            The views and the materialized views.
        """
        literals = connection.dialect.literals
        names_sql = ", ".join(literals.get_string_literal_sql(view.name) for view in (*views, *materialized_views))
        if not names_sql:
            return (), ()
        rows = await connection.execute_dicts(CLICKHOUSE_VIEWS_SQL.format(names=names_sql), [schema])
        rows_by_name = {str(row["name"]): row for row in rows}
        observed_views = []
        for view in views:
            row = rows_by_name.get(view.name)
            if row is not None and row["engine"] == CLICKHOUSE_VIEW_ENGINE:
                observed_views.append(await cls.fetch_observed_view(connection, view, row))
        observed_materialized_views = []
        for materialized_view in materialized_views:
            row = rows_by_name.get(materialized_view.name)
            if row is not None and row["engine"] == CLICKHOUSE_MATERIALIZED_VIEW_ENGINE:
                observed_materialized_views.append(
                    cls.get_observed_materialized_view(
                        await cls.fetch_observed_view(connection, materialized_view, row),
                        schema,
                        str(row["create_table_query"]),
                    )
                )
        return tuple(observed_views), tuple(observed_materialized_views)

    @classmethod
    async def fetch_observed_view[ViewType: View](
        cls, connection: DatabaseClient, view: ViewType, row: dict[str, Any]
    ) -> ViewType:
        """A declared view with the query the database keeps, where the two read differently.

        Args:
            connection: The connection.
            view: The declared view.
            row: The view's row of ``system.tables``.

        Returns:
            The view.
        """
        observed_sql = str(row["as_select"])
        if await cls.queries_are_same(connection, view.get_query_sql(connection), observed_sql):
            return view
        return dataclasses.replace(view, query=RawSQLTerm(observed_sql))

    @staticmethod
    async def queries_are_same(connection: DatabaseClient, declared_sql: str, observed_sql: str) -> bool:
        """Whether two queries read the same - compared as the server reads them, each name found.

        Args:
            connection: The connection.
            declared_sql: The declared query.
            observed_sql: The query the database keeps.

        Returns:
            Whether they are one query; False when the server can't read one of them.
        """
        if declared_sql.strip() == observed_sql.strip():
            return True
        query_trees = []
        for query_sql in (declared_sql, observed_sql):
            try:
                rows = await connection.execute_dicts(CLICKHOUSE_QUERY_TREE_SQL.format(query=query_sql))
            except DatabaseError:
                return False
            query_trees.append([tuple(row.values()) for row in rows])
        return query_trees[0] == query_trees[1]

    @classmethod
    def get_observed_materialized_view(
        cls, view: MaterializedView, schema: str, definition_sql: str
    ) -> MaterializedView:
        """A declared materialized view with the storage and the schedule the database keeps, where
        they aren't the declared ones.

        Args:
            view: The declared view, with the query of the database.
            schema: The database of the view.
            definition_sql: The view's ``CREATE MATERIALIZED VIEW``, as the server keeps it.

        Returns:
            The view itself when the database keeps it as declared, else one of ClickHouse's own.
        """
        declared = ClickhouseMaterializedView.from_materialized_view(view)
        query_index = ClickhouseSqlParts.find_words(definition_sql, CLICKHOUSE_VIEW_QUERY_WORD)
        clauses = ClickhouseSqlParts.get_clauses(
            definition_sql[: query_index if query_index >= 0 else len(definition_sql)],
            CLICKHOUSE_MATERIALIZED_VIEW_CLAUSES,
        )
        engine_clauses = ClickhouseSqlParts.get_clauses(
            clauses.get(CLICKHOUSE_ENGINE_CLAUSE, "").removeprefix("=").strip(), CLICKHOUSE_ENGINE_CLAUSES
        )
        refresh_sql = ClickhouseSqlParts.get_before_parentheses(clauses.get(CLICKHOUSE_REFRESH_CLAUSE, ""))
        target_sql = ClickhouseSqlParts.get_before_parentheses(clauses.get(CLICKHOUSE_TO_CLAUSE, ""))
        depended_sqls = ClickhouseSqlParts.split(
            ClickhouseSqlParts.get_before_parentheses(clauses.get(CLICKHOUSE_DEPENDS_ON_CLAUSE, ""))
        )
        sort_sql = engine_clauses.get(CLICKHOUSE_ORDER_BY_CLAUSE, "")
        if sort_sql.startswith("("):
            sort_sql = ClickhouseSqlParts.get_parenthesised(sort_sql)[0]
        sort_key_sqls = [] if sort_sql == CLICKHOUSE_NO_SORT_SQL else ClickhouseSqlParts.split(sort_sql)
        partition_sql = engine_clauses.get(CLICKHOUSE_PARTITION_BY_CLAUSE, "")
        observed: dict[str, Any] = {
            "to": cls.get_table_name(target_sql, schema) if target_sql else None,
            "engine": declared.engine if target_sql else engine_clauses[""],
            "order_by": tuple(
                ClickhouseSqlParts.get_identifier(key_sql) or RawSQLTerm(key_sql) for key_sql in sort_key_sqls
            ),
            "partition_by": RawSQLTerm(partition_sql) if partition_sql else None,
            "refresh": refresh_sql or None,
            "append": CLICKHOUSE_APPEND_CLAUSE in clauses,
            "depends_on": tuple(cls.get_table_name(depended_sql, schema) for depended_sql in depended_sqls),
        }
        same = {
            "to": declared.to == observed["to"],
            "engine": cls.get_compared_sql(declared.engine).removesuffix("()")
            == cls.get_compared_sql(observed["engine"]).removesuffix("()"),
            "order_by": [cls.get_compared_key(key) for key in declared.get_sort_keys()]
            == [cls.get_compared_key(key) for key in observed["order_by"]],
            "partition_by": cls.get_compared_key(declared.partition_by)
            == cls.get_compared_key(observed["partition_by"]),
            "refresh": cls.get_compared_schedule(declared.refresh) == cls.get_compared_schedule(observed["refresh"]),
            "append": declared.append == observed["append"],
            "depends_on": declared.depends_on == observed["depends_on"],
        }
        if all(same.values()):
            return view
        return dataclasses.replace(
            declared, **{option: value for option, value in observed.items() if not same[option]}
        )

    @staticmethod
    def get_table_name(table_sql: str, schema: str) -> str:
        """A table as a view of the same database names it.

        Args:
            table_sql: The table, as the server writes it - with its database.
            schema: The database of the view.

        Returns:
            The table's name; with its database where that is another one.
        """
        name_parts = [
            ClickhouseSqlParts.get_identifier(part) or part for part in ClickhouseSqlParts.split(table_sql, ".")
        ]
        return name_parts[-1] if name_parts[:-1] in ([], [schema]) else ".".join(name_parts)

    @staticmethod
    def get_compared_sql(sql: str) -> str:
        """SQL two spellings of which are compared by - without its spaces."""
        return "".join(sql.split())

    @classmethod
    def get_compared_key(cls, key: str | RawSQLTerm | None) -> str:
        """A key of a view's storage two spellings of which are compared by.

        Args:
            key: A column, an expression, or None for no key.

        Returns:
            The column's name, or the expression without its spaces.
        """
        if key is None:
            return ""
        key_sql = key.sql if isinstance(key, RawSQLTerm) else key
        return ClickhouseSqlParts.get_identifier(key_sql) or cls.get_compared_sql(key_sql)

    @staticmethod
    def get_compared_schedule(schedule: str | None) -> str:
        """A refresh schedule two spellings of which are compared by.

        Args:
            schedule: The schedule, or None for a view without one.

        Returns:
            The schedule in capitals, single spaces between its words, each unit in the singular.
        """
        if schedule is None:
            return ""
        return CLICKHOUSE_SCHEDULE_PLURAL_PATTERN.sub("", " ".join(schedule.upper().split()))
