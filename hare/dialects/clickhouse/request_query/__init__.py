"""The request query of a model on this dialect - it may use the fields and lookups only the dialect runs.
Needs ``hare-orm[request-query]``, as ``hare.contrib.request_query`` does."""

from __future__ import annotations

from hare.dialects.clickhouse.request_query.declarations import ClickhouseRequestQuery

__all__ = ("ClickhouseRequestQuery",)
