"""Request queries of one dialect - each may use the lookups only its dialect runs."""

from hare.contrib.request_query.dialects.declarations import PostgresqlRequestQuery, SqliteRequestQuery

__all__ = ("PostgresqlRequestQuery", "SqliteRequestQuery")
