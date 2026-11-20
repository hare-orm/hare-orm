from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.enums import ClickhouseClause
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_PROJECTION_ORDERS
from hare.dialects.clickhouse.schema.messages import (
    PROJECTION_COMBINED_QUERIES,
    PROJECTION_UNSUPPORTED_ORDER,
    PROJECTION_UNSUPPORTED_SHAPE,
    PROJECTION_WRONG_MODEL,
)
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.clickhouse.schema_objects.clickhouse_projection import ClickhouseProjection
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset import QuerySet


class ClickhouseProjectionCompiler:
    """Compile a declaration without creating an execution query or publishing a query plan.

    A fresh statement compiler builds a local SQL tree with values inline. Only that
    tree is adapted for the projection grammar; the queryset, cached statement plans
    and their SQL trees remain unchanged. Raw declarations are returned verbatim.
    """

    @staticmethod
    def compile(
        projection: ClickhouseProjection,
        client: DatabaseClient | None = None,
        binding: ModelBinding | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        """Return the SELECT text and read columns, validating the supported projection shape."""
        if binding is not None:
            projection = projection.for_binding(binding)
        if isinstance(projection.query, RawSQLTerm):
            return projection.query.sql, projection.read_columns
        queryset: QuerySet[Any, Any] = projection.get_queryset()  # type: ignore[assignment]
        if binding is not None and queryset._binding is not binding:
            raise ConfigurationError(
                PROJECTION_WRONG_MODEL.format(
                    model=binding._model.__name__, name=projection.name, query_model=queryset.model.__name__
                )
            )
        if queryset._combination is not None:
            raise ConfigurationError(PROJECTION_COMBINED_QUERIES.format(name=projection.name))
        from hare.query.query_connection import QueryConnection

        queryset = QueryConnection.get_schema_queryset(queryset, client)
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(queryset._context):
            compiler = queryset._get_compiler()
            # An embedded build does not read or record a runnable statement plan.
            compiler._make_query(value_wrapper_references=[])
            builder = compiler.query
            if (
                builder._wheres
                or builder._havings
                or builder._joins
                or builder._limit is not None
                or builder._offset is not None
                or builder._distinct
                or builder._distinct_on
                or builder._with
                or builder._table_sample is not None
                or builder._for_update
                or any(
                    clause != ClickhouseClause.SETTINGS or value for clause, value in builder._dialect_clauses.items()
                )
            ):
                raise ConfigurationError(
                    PROJECTION_UNSUPPORTED_SHAPE.format(model=queryset.model.__name__, name=projection.name)
                )
            if any(order not in CLICKHOUSE_PROJECTION_ORDERS for _, order in builder._orderbys):
                raise ConfigurationError(
                    PROJECTION_UNSUPPORTED_ORDER.format(model=queryset.model.__name__, name=projection.name)
                )
            read_columns = tuple(sorted({column for _, column in builder.get_read_columns()}))
            projection_query = builder.without_from()
            projection_query._orderbys = [(term, None) for term, _ in builder._orderbys]
            return str(projection_query.get_sql()), read_columns
