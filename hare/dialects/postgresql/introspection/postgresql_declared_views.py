from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view import View
from hare.dialects.postgresql.constants import (
    POSTGRESQL_COMPARED_VIEW_DEFINITION_TEMPLATE,
    POSTGRESQL_COMPARED_VIEW_NAME,
    POSTGRESQL_CREATE_COMPARED_VIEW_TEMPLATE,
    POSTGRESQL_DECLARED_VIEWS_SQL,
    POSTGRESQL_DROP_COMPARED_VIEW_TEMPLATE,
    POSTGRESQL_UNIQUE_INDEX_COLUMNS_SQL,
)
from hare.exceptions import DatabaseError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class PostgresqlDeclaredViews:
    """The views a model declares as PostgreSQL has them: a view the database lacks is left out, one
    defined another way comes as the database defines it, one meaning the same stays as declared - the
    server keeps a query in its own words, which the declared query is put in through a temporary
    view."""

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
            schema: The schema of the model.
            views: The views the model declares.
            materialized_views: Its materialized views.

        Returns:
            The views and the materialized views.
        """
        names = [view.name for view in (*views, *materialized_views)]
        if not names:
            return (), ()
        rows = await connection.execute_dicts(POSTGRESQL_DECLARED_VIEWS_SQL, [schema, names])
        rows_by_name = {(row["name"], row["is_materialized"]): row for row in rows}
        observed_views = [
            await cls.get_observed_view(connection, view, rows_by_name[(view.name, False)]["definition"])
            for view in views
            if (view.name, False) in rows_by_name
        ]
        observed_materialized_views = []
        for materialized_view in materialized_views:
            row = rows_by_name.get((materialized_view.name, True))
            if row is None:
                continue
            observed_view = await cls.get_observed_view(connection, materialized_view, row["definition"])
            observed_materialized_views.append(
                await cls.get_observed_unique_columns(connection, schema, observed_view)
            )
        return tuple(observed_views), tuple(observed_materialized_views)

    @classmethod
    async def get_observed_view[ViewType: View](
        cls, connection: DatabaseClient, view: ViewType, observed_sql: str
    ) -> ViewType:
        """A declared view with the query the database keeps, where the two read differently.

        Args:
            connection: The connection.
            view: The declared view.
            observed_sql: The view's definition, as ``pg_get_viewdef()`` writes it.

        Returns:
            The view.
        """
        declared_definition = await cls.get_definition(connection, view.get_query_sql(connection))
        if declared_definition is None or declared_definition.strip() == observed_sql.strip():
            return view
        return dataclasses.replace(view, query=RawSQLTerm(observed_sql))

    @staticmethod
    async def get_definition(connection: DatabaseClient, query_sql: str) -> str | None:
        """A query as PostgreSQL writes it - defined as a temporary view and read back, in a
        transaction of its own.

        Args:
            connection: The connection.
            query_sql: The query.

        Returns:
            The definition; None when the server can't define the query now.
        """
        create_sql = POSTGRESQL_CREATE_COMPARED_VIEW_TEMPLATE.format(
            name=POSTGRESQL_COMPARED_VIEW_NAME, query=query_sql
        )
        try:
            async with connection._in_transaction() as transaction_connection:
                await transaction_connection.execute_script(create_sql)
                rows = await transaction_connection.execute_dicts(
                    POSTGRESQL_COMPARED_VIEW_DEFINITION_TEMPLATE.format(name=POSTGRESQL_COMPARED_VIEW_NAME)
                )
                await transaction_connection.execute_script(
                    POSTGRESQL_DROP_COMPARED_VIEW_TEMPLATE.format(name=POSTGRESQL_COMPARED_VIEW_NAME)
                )
        except DatabaseError:
            return None
        return str(rows[0]["definition"])

    @staticmethod
    async def get_observed_unique_columns(
        connection: DatabaseClient, schema: str, view: MaterializedView
    ) -> MaterializedView:
        """A materialized view with the unique index the database has over its columns, where it
        isn't the declared one.

        Args:
            connection: The connection.
            schema: The schema of the view.
            view: The materialized view.

        Returns:
            The view.
        """
        rows = await connection.execute_dicts(POSTGRESQL_UNIQUE_INDEX_COLUMNS_SQL, [schema, view.name])
        unique_keys: list[tuple[Any, ...]] = [tuple(row["columns"]) for row in rows]
        if not view.unique_columns and not unique_keys:
            return view
        if any(set(unique_key) == set(view.unique_columns) for unique_key in unique_keys):
            return view
        return dataclasses.replace(view, unique_columns=unique_keys[0] if unique_keys else ())
