from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hare.dialects.base.transactions.transaction_statements import TransactionStatements
from hare.dialects.postgresql.transactions.constants import (
    POSTGRES_SET_LOCAL_LOCK_TIMEOUT_SQL,
    POSTGRES_SET_LOCAL_STATEMENT_TIMEOUT_SQL,
    POSTGRES_SET_SESSION_LOCK_TIMEOUT_SQL,
    POSTGRESQL_ALL_TENANTS_SETTING_VALUE,
    POSTGRESQL_SET_TENANT_SETTING_TEMPLATE,
    POSTGRESQL_TENANT_SETTING_NAME,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class PostgresqlTransactionStatements(TransactionStatements):
    """PostgreSQL's transaction setup - transaction-local statement and lock timeouts, and the
    transaction's tenants for row level security."""

    def get_begin_sql(self) -> str:
        return "BEGIN"

    def get_statement_timeout_sql(self, milliseconds: int) -> str | None:
        return POSTGRES_SET_LOCAL_STATEMENT_TIMEOUT_SQL.format(milliseconds=milliseconds)

    def get_lock_timeout_sql(self, milliseconds: int) -> str | None:
        return POSTGRES_SET_LOCAL_LOCK_TIMEOUT_SQL.format(milliseconds=milliseconds)

    def get_session_lock_timeout_sql(self, milliseconds: int) -> str | None:
        return POSTGRES_SET_SESSION_LOCK_TIMEOUT_SQL.format(milliseconds=milliseconds)

    def get_tenant_setting_sql(self, tenant_texts: Sequence[str] | None) -> str:
        value = POSTGRESQL_ALL_TENANTS_SETTING_VALUE if tenant_texts is None else json.dumps(list(tenant_texts))
        return POSTGRESQL_SET_TENANT_SETTING_TEMPLATE.format(
            setting=POSTGRESQL_TENANT_SETTING_NAME, value=self.dialect.literals.get_string_literal_sql(value)
        )

    def get_table_tenants_setting_sql(self, table_tenant_texts: Mapping[str, Sequence[str] | None]) -> str:
        value = json.dumps(
            {
                table_key: POSTGRESQL_ALL_TENANTS_SETTING_VALUE if tenant_texts is None else list(tenant_texts)
                for table_key, tenant_texts in table_tenant_texts.items()
            }
        )
        return POSTGRESQL_SET_TENANT_SETTING_TEMPLATE.format(
            setting=POSTGRESQL_TENANT_SETTING_NAME, value=self.dialect.literals.get_string_literal_sql(value)
        )
