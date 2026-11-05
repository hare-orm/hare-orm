from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

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
    from hare.models import Model
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
        model: type[Model] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        """Return the SELECT text and read columns, validating the supported projection shape."""
        if model is not None:
            projection = projection.for_model(model)
        if isinstance(projection.query, RawSQLTerm):
            return projection.query.sql, projection.read_columns
        queryset = cast("QuerySet[Any, Any]", projection.get_queryset())
        if model is not None and queryset.model is not model:
            raise ConfigurationError(
                PROJECTION_WRONG_MODEL.format(
                    model=model.__name__, name=projection.name, query_model=queryset.model.__name__
                )
            )
        if queryset._combination is not None:
            raise ConfigurationError(PROJECTION_COMBINED_QUERIES.format(name=projection.name))
        compiler = queryset.using(client or queryset.get_connection())._get_compiler()
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
            or any(clause != ClickhouseClause.SETTINGS or value for clause, value in builder._dialect_clauses.items())
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
