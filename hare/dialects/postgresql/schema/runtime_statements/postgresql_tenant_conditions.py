from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.runtime_statements.tenant_conditions import TenantConditions
from hare.dialects.postgresql.schema.constants import POSTGRESQL_TENANT_CONDITION_TEMPLATE
from hare.dialects.postgresql.transactions.constants import (
    POSTGRESQL_ALL_TENANTS_SETTING_VALUE,
    POSTGRESQL_TENANT_SETTING_NAME,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTenantConditions(TenantConditions):
    """TenantConditions as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    @classmethod
    def get_tenant_condition_sql(cls, quoted_column: str, column_type: str) -> str:
        return POSTGRESQL_TENANT_CONDITION_TEMPLATE.format(
            column=quoted_column,
            column_type=column_type,
            setting=POSTGRESQL_TENANT_SETTING_NAME,
            all_tenants=POSTGRESQL_ALL_TENANTS_SETTING_VALUE,
        )

    @classmethod
    def get_set_column_count_sql(cls, quoted_columns: list[str]) -> str:
        return f"num_nonnulls({', '.join(quoted_columns)})"
