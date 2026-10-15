"""Tenancy enforced by PostgreSQL: a ``Policy(using=TenantCondition())`` keeps a ``Meta.tenant_field``
model's rows to the tenants of the transaction, which a connection with ``tenant_row_level_security``
sets right after ``BEGIN`` from the active ``Tenancy.scope()``."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError, OperationalError, QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.models_tenant_row_level_security import RlsLabel, RlsNote

MODULE = "tests.dialects.postgresql.models_tenant_row_level_security"


def get_db_url(*, row_level_security: bool) -> str:
    db_url = os.environ["HARE_TEST_DB"]
    if not row_level_security:
        return db_url
    return f"{db_url}{'&' if '?' in db_url else '?'}tenant_row_level_security=true"


@asynccontextmanager
async def tenant_context(*, row_level_security: bool = True):
    async with hare_test_context(
        modules=[MODULE],
        db_url=get_db_url(row_level_security=row_level_security),
        app_label="models",
        connection_label="models",
        reuse_databases=False,
    ) as ctx:
        connection = Connections.get("models")
        _, rows = await connection.execute("SELECT current_database() AS name")
        role = f"hare_rls_{rows[0]['name']}"[:63]
        await connection.execute_script(
            f'DROP ROLE IF EXISTS "{role}"; CREATE ROLE "{role}" NOLOGIN; '
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON rls_note, rls_label TO "{role}"; '
            "INSERT INTO rls_note VALUES (1, 1, 'one'), (2, 2, 'two'), (3, 3, 'three'); "
            "INSERT INTO rls_label VALUES (1, 'acme'), (2, 'beta')"
        )
        try:
            yield ctx, role
        finally:
            await connection.execute_script(f'DROP OWNED BY "{role}"; DROP ROLE "{role}"')


async def as_role(connection: DatabaseClient, role: str) -> None:
    # The test database's user is a superuser, which row level security never applies to.
    await connection.execute(f'SET LOCAL ROLE "{role}"')


async def count_rows(connection: DatabaseClient, table: str) -> int:
    _, rows = await connection.execute(f"SELECT count(*) AS row_count FROM {table}")
    return rows[0]["row_count"]


@pytest.mark.asyncio
async def test_the_database_keeps_a_transaction_to_its_tenants():
    skip_if_not_postgres()
    async with tenant_context() as (_, role):
        with Tenancy.scope(1):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                # Raw SQL carries no tenant filter - the policy alone keeps the other tenants out.
                assert await count_rows(connection, "rls_note") == 1
                assert [obj.text for obj in await RlsNote.objects.all()] == ["one"]
                await RlsNote.objects.create(id=4, text="four")
                with pytest.raises(OperationalError, match="row-level security"):
                    async with Transactions.atomic("models") as savepoint:
                        await savepoint.execute("INSERT INTO rls_note VALUES (5, 2, 'five')")
        with Tenancy.scope(Tenancy.any_of(1, 2)):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                assert await count_rows(connection, "rls_note") == 3
        with Tenancy.scope(Tenancy.ALL):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                assert await count_rows(connection, "rls_note") == 4
        with Tenancy.scope("acme"):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                assert await count_rows(connection, "rls_label") == 1
                assert [obj.id for obj in await RlsLabel.objects.all()] == [1]
        with Tenancy.scope("o'brien"):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                assert await count_rows(connection, "rls_label") == 0


@pytest.mark.asyncio
async def test_a_transaction_with_no_tenant_sees_no_row():
    skip_if_not_postgres()
    async with tenant_context() as (_, role):
        async with Transactions.atomic("models") as connection:
            await as_role(connection, role)
            assert await count_rows(connection, "rls_note") == 0
            with pytest.raises(QueryError, match="no tenant scope"):
                await RlsNote.objects.all_tenants().count()
        with Tenancy.scope({RlsNote: 1}):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                assert await count_rows(connection, "rls_note") == 0
                with pytest.raises(QueryError, match="no tenant scope"):
                    await RlsNote.objects.count()


@pytest.mark.asyncio
async def test_a_query_runs_in_a_transaction_of_one_scope():
    skip_if_not_postgres()
    async with tenant_context():
        with Tenancy.scope(1):
            with pytest.raises(QueryError, match="inside a transaction"):
                await RlsNote.objects.count()
            async with Transactions.atomic("models"):
                assert await RlsNote.objects.count() == 1
                async with Transactions.atomic("models"):
                    assert await RlsNote.objects.count() == 1
                with Tenancy.scope(2), pytest.raises(QueryError, match="began in"):
                    await RlsNote.objects.count()


@pytest.mark.asyncio
async def test_a_connection_without_the_option_refuses_the_model():
    skip_if_not_postgres()
    async with tenant_context(row_level_security=False):
        with Tenancy.scope(1):
            async with Transactions.atomic("models"):
                with pytest.raises(ConfigurationError, match="no tenant_row_level_security"):
                    await RlsNote.objects.count()


@pytest.mark.asyncio
async def test_a_tenant_condition_needs_a_tenant_field():
    with pytest.raises(ConfigurationError, match="Meta.tenant_field"):
        async with hare_test_context(
            modules=["tests.dialects.postgresql.models_tenant_row_level_security_no_field"],
            app_label="models",
            connection_label="models",
        ):
            pass


def test_the_policy_and_the_setting_sql():
    dialect: Any = DialectRegistry.get_dialect("postgresql")
    condition_sql = dialect.schema_editor_class.tenant_conditions_class.get_tenant_condition_sql('"company_id"', "INT")
    assert condition_sql == (
        "CASE current_setting('hare.tenant', true) WHEN '*' THEN true ELSE \"company_id\" = ANY "
        "(ARRAY(SELECT jsonb_array_elements_text(NULLIF(current_setting('hare.tenant', true), '')::jsonb))"
        "::INT[]) END"
    )
    assert (
        dialect.transactions.get_tenant_setting_sql(["7", "o'brien"])
        == "SELECT set_config('hare.tenant', '[\"7\", \"o''brien\"]', true)"
    )
    assert dialect.transactions.get_tenant_setting_sql(None) == "SELECT set_config('hare.tenant', '*', true)"
    sqlite_dialect: Any = DialectRegistry.get_dialect("sqlite")
    with pytest.raises(Exception, match="row level security"):
        sqlite_dialect.transactions.get_tenant_setting_sql(["7"])
