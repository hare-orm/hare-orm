from typing import TYPE_CHECKING, Any

from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.sql.queries.builder.query import Query

if TYPE_CHECKING:
    from hare.dialects.sqlite.query.sqlite_query_builder import SqliteQueryBuilder


class SqliteQuery(Query):
    """Defines a query class for use with SQLite."""

    SQL_CONTEXT = SQLITE_DIALECT.sql_context

    @classmethod
    def _builder(cls, **kwargs: Any) -> SqliteQueryBuilder:
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.query.sqlite_query_builder import SqliteQueryBuilder

        return SqliteQueryBuilder(**kwargs)
