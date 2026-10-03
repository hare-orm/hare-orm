from hare.dialects.postgresql.query.postgresql_query import PostgresqlQuery
from hare.sql.context import SqlContext
from hare.sql.queries.builder.query_builder import QueryBuilder


class PostgresqlQueryBuilder(QueryBuilder):
    QUERY_CLS = PostgresqlQuery

    def _distinct_sql(self, ctx: SqlContext) -> str:
        distinct_ctx = ctx.copy(with_alias=True)
        if self._distinct_on:
            return "DISTINCT ON({distinct_on}) ".format(
                distinct_on=",".join(term.get_sql(distinct_ctx) for term in self._distinct_on)
            )
        return super()._distinct_sql(distinct_ctx)

    def get_sql(self, ctx: SqlContext | None = None) -> str:
        return self._get_sql_with_self_aliased_update_and_returning(ctx)

    def _for_update_sql(self, ctx: SqlContext, lock_strength="UPDATE") -> str:
        if self._for_update and self._for_update_no_key:
            lock_strength = "NO KEY UPDATE"
        return super()._for_update_sql(ctx, lock_strength=lock_strength)
