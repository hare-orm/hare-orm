from __future__ import annotations

from hare.dialects.base.client import ConnectionWrapper, DatabaseClient
from hare.dialects.base.client.statement_recovery import StatementRecovery
from hare.dialects.base.results import StatementResult
from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext
from hare.dialects.dialect_registry import DialectRegistry
from hare.sql.sql_context import SqlContext
from hare.transactions.transaction_options import TransactionOptions


class FakeClient(DatabaseClient):
    def __init__(self, dialect: str, *, inline_comment: bool = True, charset: str | None = None) -> None:
        super().__init__("default")
        self.dialect = DialectRegistry.get_dialect(dialect)
        # No transactions - its DDL runs as a script, as on a database that can't roll DDL back.
        self.features = self.dialect.features.replace(
            inline_comments=inline_comment, supports_transactions=False, can_rollback_ddl=False
        )
        self.cluster = None
        self.database = None
        self.legacy_alter_table = 0
        self.charset = charset
        self.executed: list[str] = []

    @property
    def sql_context(self) -> SqlContext:
        return self.dialect.sql_context.copy(features=self.features)

    async def create_connection(self, with_db: bool) -> None:
        raise NotImplementedError()

    async def close(self) -> None:
        raise NotImplementedError()

    async def db_create(self) -> None:
        raise NotImplementedError()

    async def db_delete(self) -> None:
        raise NotImplementedError()

    def acquire_connection(self) -> ConnectionWrapper:
        raise NotImplementedError()

    def _in_transaction(self, options: TransactionOptions = TransactionOptions.DEFAULT) -> TransactionContext:
        raise NotImplementedError()

    async def execute(
        self,
        query: str,
        values: list | None = None,
        *,
        returns_rows: bool | None = None,
        rows_by_position: bool = False,
        retry_read: bool = False,
        recovery: StatementRecovery | None = None,
    ) -> StatementResult:
        if query == "PRAGMA legacy_alter_table":
            return StatementResult(1, [(self.legacy_alter_table,)])
        if query in (
            "PRAGMA legacy_alter_table = ON",
            "PRAGMA legacy_alter_table = 1",
            "PRAGMA legacy_alter_table = 0",
        ):
            self.legacy_alter_table = int(not query.endswith("0"))
            return StatementResult(0, [])
        raise NotImplementedError(query)

    async def execute_script(self, query: str, *, recovery: StatementRecovery | None = None) -> None:
        self.executed.append(query)

    async def execute_many(self, query: str, values: list[list], *, recovery: StatementRecovery | None = None) -> None:
        raise NotImplementedError()
