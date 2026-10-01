from __future__ import annotations

from typing import Any

from hare.dialects.base.client import DatabaseClient
from hare.dialects.registry import DialectRegistry
from hare.dialects.sqlite.client import SqliteClient
from hare.dialects.sqlite.driver import SqliteDriver
from hare.dialects.sqlite.query import SqliteQuery, SqliteQueryBuilder
from tests.dialects.columnar.dialect import COLUMNAR_DIALECT


class ColumnarQuery(SqliteQuery):
    """SQLite's query, rendered for the columnar dialect - backtick-quoted identifiers."""

    SQL_CONTEXT = COLUMNAR_DIALECT.sql_context

    @classmethod
    def _builder(cls, **kwargs: Any) -> ColumnarQueryBuilder:
        return ColumnarQueryBuilder(**kwargs)


class ColumnarQueryBuilder(SqliteQueryBuilder):
    QUERY_CLS = ColumnarQuery


class ColumnarClient(SqliteClient):
    """A SQLite connection speaking the columnar dialect: no transactions - a block hare opens for
    a multi-statement write runs its statements one by one."""

    driver_name = "columnar"
    dialect = COLUMNAR_DIALECT
    query_class = ColumnarQuery
    features = SqliteClient.features.replace(supports_transactions=False, can_rollback_ddl=False)


class ColumnarDriver(SqliteDriver):
    """The columnar dialect through the stdlib ``sqlite3`` module - ``columnar://<file or :memory:>``."""

    name = "columnar"
    dialect = COLUMNAR_DIALECT
    url_schemes = ("columnar",)

    def get_client_class(self, credentials: dict[str, Any]) -> type[ColumnarClient]:
        return ColumnarClient

    def get_client_classes(self) -> tuple[type[DatabaseClient], ...]:
        return (ColumnarClient,)


COLUMNAR_DRIVER = ColumnarDriver()
DialectRegistry.register_driver(COLUMNAR_DRIVER)
