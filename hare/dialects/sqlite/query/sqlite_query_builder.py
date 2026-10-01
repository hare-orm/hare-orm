from hare.dialects.sqlite.query.sqlite_query import SqliteQuery
from hare.dialects.sqlite.query.sqlite_value_wrapper import SqliteValueWrapper
from hare.sql.context import SqlContext
from hare.sql.queries.builder.query_builder import QueryBuilder


class SqliteQueryBuilder(QueryBuilder):
    QUERY_CLS = SqliteQuery

    def __init__(self, **kwargs) -> None:
        super().__init__(wrapper_cls=SqliteValueWrapper, **kwargs)

    def get_sql(self, ctx: SqlContext | None = None) -> str:
        return self._get_sql_with_self_aliased_update_and_returning(ctx)
