"""The request query of a model on this dialect - it may use the lookups only the dialect runs.
Needs ``hare-orm[request-query]``, as ``hare.contrib.request_query`` does."""

from __future__ import annotations

from hare.dialects.postgresql.request_query.declarations import PostgresqlRequestQuery

__all__ = ("PostgresqlRequestQuery",)
