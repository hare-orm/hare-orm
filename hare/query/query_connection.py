from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, TypeVar

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.constants import SQL_DIALECT
from hare.dialects.base.dialect import Dialect
from hare.exceptions import QueryError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.query_specification import QuerySpecification


BoundSpecification = TypeVar("BoundSpecification", bound="QuerySpecification[Any]")


class QueryConnection:
    """The connection a query is built for: the one it is bound to, the one a query built into another
    one is pinned to with using(), and the dialect a query is analysed with before it has a
    connection."""

    @staticmethod
    def get_bound_connection(specification: QuerySpecification[Any]) -> DatabaseClient:
        """The connection the query runs on.

        Args:
            specification: The query specification - a queryset or a query made from one.

        Raises:
            QueryError: No connection is chosen yet - the query isn't running.
        """
        connection = specification._connection
        if connection is None:
            raise QueryError(
                f"The {type(specification).__name__} on {specification.model.__name__} has no connection yet - "
                "the connection, its dialect and features are chosen when the query runs"
            )
        return connection

    @staticmethod
    def get_pinned_connection_name(specification: QuerySpecification[Any]) -> str | None:
        """The name of the connection the query is pinned to with ``.using()`` - None for a query
        that picks its connection when it runs.

        Args:
            specification: The query specification - a queryset or a query made from one.
        """
        connection = specification._connection
        return connection.connection_alias if connection is not None else None

    @staticmethod
    def get_bound_to(
        specification: BoundSpecification,
        outer_connection: DatabaseClient | None,
        outer_model: type[Model],
        embedded_as: str,
    ) -> BoundSpecification:
        """This query as it is built into another one - bound to the connection the outer query runs
        on, since both compile to one SQL text. Without ``.using()`` it takes the outer connection;
        pinned to the same one it is kept as is.

        Args:
            specification: The query specification - a queryset or a query made from one.
            outer_connection: The outer query's connection - None for a query compiled outside execution.
            outer_model: The outer query's model, for the message.
            embedded_as: What this query is in the outer one, for the message.

        Returns:
            A copy bound to the outer query's connection.

        Raises:
            QueryError: This query is pinned to another connection than the outer query's.
        """
        if specification._pending_filter_calls:
            # Built once, so every copy holds the same conditions (_build_conditions_for_copies()).
            # Imported here: the filter calls import the expressions, which import this module.
            from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls

            PendingFilterCalls.build_pending_filter_calls(specification)
        if outer_connection is None:
            execution_query = specification._get_execution_query()
            execution_query._plan_origin = specification
            return execution_query
        pinned_connection = specification._connection
        if pinned_connection is not None and pinned_connection.connection_alias != outer_connection.connection_alias:
            raise QueryError(
                f"{specification.model.__name__} query used as {embedded_as} is pinned to a different database "
                f"connection ({pinned_connection.connection_alias!r}) than the outer {outer_model.__name__} query "
                "runs "
                f"on ({outer_connection.connection_alias!r}) - both compile to one SQL text, run on the outer query's "
                "connection. Remove the inner queryset's .using() override, or query each connection "
                "separately instead."
            )
        bound_query = copy(specification)
        bound_query._plan_origin = specification
        bound_query._apply_connection(outer_connection)
        return bound_query

    @staticmethod
    def get_analysis_dialect(specification: QuerySpecification[Any]) -> Dialect:
        """The dialect expressions are resolved with to analyse the query's shape - the chosen
        connection's, else the neutral SQL dialect before the query runs.

        Args:
            specification: The query specification - a queryset or a query made from one.
        """
        connection = specification._connection
        return SQL_DIALECT if connection is None else connection.dialect
