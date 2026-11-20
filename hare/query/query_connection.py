from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.constants import SQL_DIALECT
from hare.dialects.base.dialect import Dialect
from hare.exceptions import ConfigurationError, QueryError

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.awaitable_query import AwaitableQuery


class QueryConnection:
    """The connection a query is built for: the one it is bound to, the one a query built into another
    one is pinned to with using(), and the dialect a query is analysed with before it has a
    connection."""

    @staticmethod
    def get_schema_queryset(queryset: QuerySet[Any, Any], client: DatabaseClient | None) -> QuerySet[Any, Any]:
        """Choose explicit schema compilation resources without reinterpreting structural origin.

        A bound source supplies its application. A detached declaration needs an explicit
        client; its caller's execution context is used only when that client has the same owner.
        Offline clients compile without application resources or global fallback.
        """
        from hare.core.hare_context import HareContext

        context = queryset._context
        if client is None:
            client = queryset._connection
        if client is None:
            if context is None:
                raise ConfigurationError("Compiling a detached schema query requires an explicit database client")
            client = queryset.get_connection()
        if context is None and client.connection_handler is not None:
            candidate = HareContext.get_current()
            if candidate is None or candidate.connections is not client.connection_handler:
                raise ConfigurationError("Schema compilation requires the explicit client's application context")
            context = candidate
        bound = queryset.using(client)
        bound._context = context
        return bound

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
    def accepts_pinned_client(outer: DatabaseClient, pinned: DatabaseClient) -> bool:
        """A statement can include a query pinned to its client or a containing transaction's client."""
        while outer is not pinned and outer.is_transaction_client:
            outer = outer._parent
        return outer is pinned

    @staticmethod
    def get_bound_to(
        specification: QuerySpecification[Any],
        outer_connection: DatabaseClient | None,
        outer_binding: ModelBinding,
        embedded_as: str,
        *,
        context: HareContext | None,
    ) -> AwaitableQuery[Any]:
        """This query as it is built into another one - bound to the connection the outer query runs
        on, since both compile to one SQL text. Without ``.using()`` it takes the outer connection;
        a declaration already pinned to that connection is copied too, keeping build state separate.

        Args:
            specification: The query specification - a queryset or a query made from one.
            outer_connection: The outer query's connection - None for a query compiled outside execution.
            outer_binding: The outer query's model binding and graph.
            embedded_as: What this query is in the outer one, for the message.
            context: The outer query context used to bind the inner declaration.

        Returns:
            A copy bound to the outer query's connection.

        Raises:
            QueryError: This query is pinned to another connection than the outer query's.
        """
        from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup

        query = CallsBeforeSetup.bind(specification, context, outer_binding.graph)._get_compiler()
        if query is not specification:
            # Description and rendering build separate compilers for the same declaration.
            query._plan_origin = specification
        outer_model = outer_binding._model
        if query._pending_filter_calls:
            # Built once, so every copy holds the same conditions (_build_conditions_for_copies()).
            # Imported here: the filter calls import the expressions, which import this module.
            from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls

            PendingFilterCalls.build_pending_filter_calls(query)
        if outer_connection is None:
            execution_query = query._get_execution_query()
            execution_query._plan_origin = query
            return execution_query
        # None until .using(), the router or the default connection sets it.
        pinned_connection: DatabaseClient | None = query._connection
        if pinned_connection is not None and not QueryConnection.accepts_pinned_client(
            outer_connection, pinned_connection
        ):
            raise QueryError(
                f"{query.model.__name__} query used as {embedded_as} is pinned to a different database "
                f"connection ({pinned_connection.connection_alias!r}) than the outer {outer_model.__name__} query "
                "runs "
                f"on ({outer_connection.connection_alias!r}) - both compile to one SQL text, run on the outer query's "
                "connection. Remove the inner queryset's .using() override, or query each connection "
                "separately instead."
            )
        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        if pinned_connection is None:
            QueryConnection.raise_if_model_on_another_connection(
                model_binding, outer_connection, outer_binding, embedded_as, context=context
            )
        meta: ModelBinding = model_binding
        if meta.checks_tenant_client:
            meta.check_tenant_client(outer_connection)
        bound_query = copy(query)
        bound_query._plan_origin = query
        bound_query._apply_connection(outer_connection)
        return bound_query

    @staticmethod
    def raise_if_model_on_another_connection(
        binding: ModelBinding,
        outer_connection: DatabaseClient,
        outer_binding: ModelBinding,
        embedded_as: str,
        *,
        context: HareContext | None,
    ) -> None:
        """Refuses a query of a model living on another connection than the outer query, which runs on its
        own model's connection - bound to the outer one, it would read that connection's table of its name.
        An outer query sent to a connection of the caller's choice (a shard) takes the inner query along.

        Args:
            binding: The inner query's model binding.
            outer_connection: The outer query's connection.
            outer_binding: The outer query's model binding.
            embedded_as: What the inner query is in the outer one, for the message.
            context: The context used to check routing for both models, if available.

        Raises:
            QueryError: The inner model lives on another connection.
        """
        from hare.core.connections.model_connection_choice import ModelConnectionChoice

        if binding is outer_binding or context is None:
            return
        outer_alias = outer_connection.connection_alias
        routed_aliases_of = {
            candidate: {
                ModelConnectionChoice.get_unchecked(context, candidate, for_write).connection_alias
                for for_write in (False, True)
            }
            for candidate in (binding, outer_binding)
        }
        if outer_alias in routed_aliases_of[outer_binding] and outer_alias not in routed_aliases_of[binding]:
            raise QueryError(
                f"{binding._model.__name__} query used as {embedded_as} runs on a different database connection "
                f"({sorted(routed_aliases_of[binding])[0]!r}) than the outer {outer_binding._model.__name__} query "
                f"runs on "
                f"({outer_alias!r}) - both compile to one SQL text. Query each connection separately instead."
            )

    @staticmethod
    def get_analysis_dialect(specification: QuerySpecification[Any]) -> Dialect:
        """The dialect expressions are resolved with to analyse the query's shape - the chosen
        connection's, else the neutral SQL dialect before the query runs.

        Args:
            specification: The query specification - a queryset or a query made from one.
        """
        connection = specification._connection
        return SQL_DIALECT if connection is None else connection.dialect
