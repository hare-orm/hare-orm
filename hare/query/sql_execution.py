from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, overload

from hare.core.connections import Connections
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import QueryError
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.utils.pydantic_classes import PydanticClasses

if TYPE_CHECKING:  # pragma: nocoverage
    from pydantic import TypeAdapter as PydanticTypeAdapter

SchemaT = TypeVar("SchemaT")


@dataclass(frozen=True)
class SqlQueryResult(Generic[SchemaT]):
    rows: list[SchemaT]
    rows_affected: int
    """
    For SELECT (and a write with RETURNING), the number of rows fetched; for INSERT/UPDATE/DELETE,
    the rows the statement itself changed - rows changed by triggers or foreign-key cascades it set
    off are not counted, on any backend.
    """

    @classmethod
    def validate_rows(cls, rows: list[dict[str, Any]], schema: type[SchemaT] | Any) -> list[SchemaT]:
        """The rows validated by a pydantic model or type adapter; as they are for any other schema.

        Args:
            rows: The fetched rows.
            schema: The schema.

        Returns:
            The rows.
        """
        if PydanticClasses.is_type_adapter(schema):
            return [cast("SchemaT", schema.validate_python(row)) for row in rows]
        if PydanticClasses.is_model_class(schema):
            return [cast("SchemaT", schema.model_validate(row)) for row in rows]
        return cast("list[SchemaT]", rows)


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
        db = cast("DatabaseClient", Connections.get_client(using))
    else:
        conn_handler = Connections.current()
        if len(conn_handler.db_config) == 1:
            connection_name = next(iter(conn_handler.db_config.keys()))
            db = conn_handler.get(connection_name)
        else:
            raise QueryError(
                f"You are running with multiple databases, so you should specify using: {list(conn_handler.db_config)}"
            )
    sql, params = query.get_parameterized_sql(db.query_class.SQL_CONTEXT)
    statement_result = await db.execute(sql, params)
    rows = list(map(db.row_to_dict, statement_result.rows))
    rows_affected = statement_result.row_count

    if schema is not None:
        rows = SqlQueryResult.validate_rows(rows, schema)

    return SqlQueryResult(rows=rows, rows_affected=rows_affected)
