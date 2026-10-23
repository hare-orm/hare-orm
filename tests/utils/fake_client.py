from __future__ import annotations

from hare.dialects.base.client import ConnectionWrapper, DatabaseClient
from hare.dialects.base.results import StatementResult
from hare.dialects.base.transactions.contexts.transaction_context import TransactionContext
from hare.dialects.dialect_registry import DialectRegistry
from hare.transactions.transaction_options import TransactionOptions


class FakeClient(DatabaseClient):
    def __init__(self, dialect: str, *, inline_comment: bool = True, charset: str | None = None) -> None:
        super().__init__("default")
        self.dialect = DialectRegistry.get_dialect(dialect)
        # No transactions - its DDL runs as a script, as on a database that can't roll DDL back.
        self.features = self.dialect.features.replace(
            inline_comments=inline_comment, supports_transactions=False, can_rollback_ddl=False
        )
        self.charset = charset
        self.executed: list[str] = []

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
        self, query: str, values: list | None = None, *, returns_rows: bool | None = None
    ) -> StatementResult:
        if query.startswith("PRAGMA index_list"):
            # The indexes the fake database has: the ones a CREATE INDEX made.
            return StatementResult(0, [{"name": name} for name in self.get_created_index_names()])
        raise NotImplementedError()

    def get_created_index_names(self) -> list[str]:
        """The names of the indexes the executed SQL created and didn't drop."""
        names: list[str] = []
        for statement in self.executed:
            words = statement.replace('"', " ").split()
            if statement.startswith(("CREATE INDEX", "CREATE UNIQUE INDEX")):
                names.append(words[words.index("INDEX") + 1])
            elif statement.startswith("DROP INDEX"):
                dropped = words[-1]
                names = [name for name in names if name != dropped]
        return names

    async def execute_script(self, query: str) -> None:
        self.executed.append(query)

    async def execute_many(self, query: str, values: list[list]) -> None:
        raise NotImplementedError()


class MockIntrospectionClient(FakeClient):
    """A FakeClient subclass that returns configurable results for introspection queries.

    When ``execute`` is called with a query containing known introspection
    keywords (``pg_constraint``, ``information_schema``, ``PRAGMA index_list``,
    ``PRAGMA index_info``), the client returns configured results instead of
    raising ``NotImplementedError``.
    """

    def __init__(
        self,
        dialect: str,
        *,
        constraint_names: list[dict] | None = None,
        pragma_index_list: list[dict] | None = None,
        pragma_index_info: dict[str, list[dict]] | None = None,
        inline_comment: bool = True,
        charset: str | None = None,
    ) -> None:
        super().__init__(dialect, inline_comment=inline_comment, charset=charset)
        self.constraint_names = constraint_names or []
        self.pragma_index_list = pragma_index_list or []
        self.pragma_index_info = pragma_index_info or {}

    async def execute(
        self, query: str, values: list | None = None, *, returns_rows: bool | None = None
    ) -> StatementResult:
        if (
            "pg_constraint" in query
            or "information_schema" in query
            or "sys.key_constraints" in query
            or "USER_CONSTRAINTS" in query
            or "ALL_CONSTRAINTS" in query
        ):
            return StatementResult(len(self.constraint_names), self.constraint_names)
        if "PRAGMA index_list" in query:
            return StatementResult(len(self.pragma_index_list), self.pragma_index_list)
        if "PRAGMA index_info" in query:
            # Extract the index name from the query: PRAGMA index_info("idx_name")
            idx_name = query.split('"')[1] if '"' in query else ""
            info = self.pragma_index_info.get(idx_name, [])
            return StatementResult(len(info), info)
        raise NotImplementedError()
