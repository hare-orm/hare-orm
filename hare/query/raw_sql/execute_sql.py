from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast, overload

from hare.core.connections.connections import Connections
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import QueryError
from hare.query.raw_sql.sql_query_result import SchemaT, SqlQueryResult
from hare.sql.builder.queries.query_builder import QueryBuilder

if TYPE_CHECKING:
    from pydantic import TypeAdapter as PydanticTypeAdapter


@overload
async def execute_sql(
    query: QueryBuilder,
    *,
    using: str | DatabaseClient | None = None,
    schema: None = None,
) -> SqlQueryResult[dict[str, Any]]: ...


@overload
async def execute_sql(
    query: QueryBuilder,
    *,
    using: str | DatabaseClient | None = None,
    schema: type[SchemaT],
) -> SqlQueryResult[SchemaT]: ...


@overload
async def execute_sql(
    query: QueryBuilder,
    *,
    using: str | DatabaseClient | None = None,
    schema: PydanticTypeAdapter[SchemaT],
) -> SqlQueryResult[SchemaT]: ...


async def execute_sql(
    query: QueryBuilder,
    *,
    using: str | DatabaseClient | None = None,
    schema: type[SchemaT] | PydanticTypeAdapter[SchemaT] | Any | None = None,
) -> SqlQueryResult[SchemaT] | SqlQueryResult[dict[str, Any]]:
    if using is not None:
        connection = cast("DatabaseClient", Connections.get_client(using))
    else:
        connection_handler = Connections.current()
        single_client = connection_handler.get_single_client()
        if single_client is None:
            raise connection_handler.get_ambiguous_connection_error()
        connection = single_client
    sql, parameters = query.get_parameterized_sql(connection.query_class.SQL_CONTEXT)
    statement_result = await connection.execute(sql, parameters, rows_by_position=True)
    column_names = statement_result.column_names
    if len(set(column_names)) != len(column_names):
        repeated_names = sorted({name for name in column_names if column_names.count(name) > 1})
        raise QueryError(
            f"execute_sql(): the result has several columns named {', '.join(map(repr, repeated_names))} - a row "
            "can't hold them by name; give them aliases, or read the result with execute_described()"
        )
    rows = [dict(zip(column_names, row, strict=True)) for row in statement_result.rows]
    rows_affected = statement_result.row_count

    if schema is not None:
        rows = SqlQueryResult.validate_rows(rows, schema)

    return SqlQueryResult(rows=rows, rows_affected=rows_affected)
