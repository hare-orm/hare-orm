from __future__ import annotations

from hare.dialects.base.transactions.transaction_statements import TransactionStatements
from hare.dialects.sqlite.transactions.constants import SQLITE_ENABLE_QUERY_ONLY_SQL
from hare.transactions.enums import IsolationLevel


class SqliteTransactionStatements(TransactionStatements):
    """SQLite's transaction setup - read-only through ``PRAGMA query_only``, no isolation statement,
    every SQLite transaction being serializable."""

    def get_begin_sql(self) -> str:
        return "BEGIN"

    def get_isolation_level_sql(self, level: IsolationLevel) -> str | None:
        return None

    def get_read_only_sql(self) -> str | None:
        return SQLITE_ENABLE_QUERY_ONLY_SQL
