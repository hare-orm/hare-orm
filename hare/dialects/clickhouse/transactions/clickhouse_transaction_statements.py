from __future__ import annotations

from hare.dialects.base.transactions.transaction_statements import TransactionStatements
from hare.dialects.clickhouse.client.constants import CLICKHOUSE_BEGIN_TRANSACTION_SQL
from hare.transactions.enums import IsolationLevel


class ClickhouseTransactionStatements(TransactionStatements):
    """ClickHouse's transaction setup - no isolation statement, every transaction reading the snapshot
    it began with; no read-only transaction."""

    def get_begin_sql(self) -> str:
        return CLICKHOUSE_BEGIN_TRANSACTION_SQL

    def get_isolation_level_sql(self, level: IsolationLevel) -> str | None:
        return None

    def get_read_only_sql(self) -> str | None:
        return None
