from typing import TYPE_CHECKING

from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.sql.queries.builder.query import Query

if TYPE_CHECKING:
    from hare.dialects.postgresql.query.postgresql_query_builder import PostgresqlQueryBuilder


class PostgresqlQuery(Query):
    """Defines a query class for use with PostgreSQL."""

    SQL_CONTEXT = POSTGRESQL_DIALECT.sql_context

    @classmethod
    def _builder(cls, **kwargs) -> PostgresqlQueryBuilder:
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.query.postgresql_query_builder import PostgresqlQueryBuilder

        return PostgresqlQueryBuilder(**kwargs)
