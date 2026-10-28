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
from hare.ddl import TenantCondition
from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError, OperationalError, QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.scopes.tenants.model_tenants import ModelTenants
from hare.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.tenancy.models_tenant_row_level_security import RlsLabel, RlsMention, RlsNote

MODULE = "tests.dialects.postgresql.tenancy.models_tenant_row_level_security"


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
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON rls_note, rls_label, rls_mention TO "{role}"; '
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
async def test_scopes_recheck_policy_tenants_after_live_models_change():
    skip_if_not_postgres()
    async with tenant_context() as (context, _):
        scopes = [ModelTenants({RlsNote: 1, RlsLabel: "acme"}) for _ in range(2)]
        note_key = TenantCondition.get_table_key(RlsNote)
        label_key = TenantCondition.get_table_key(RlsLabel)
        for scope in scopes:
            assert scope.get_policy_tenants("models") == {note_key: 1, label_key: "acme"}
        context.unregister_live_models([RlsLabel])
        for scope in scopes:
            assert scope.get_policy_tenants("models") == {note_key: 1}
        context.register_live_models([RlsLabel], app_label="models", connection_alias="models")
        for scope in scopes:
            assert scope.get_policy_tenants("models") == {note_key: 1, label_key: "acme"}


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


@pytest.mark.asyncio
async def test_a_scope_given_model_by_model_sets_each_table_s_tenants():
    """The transaction set no tenant under a scope given model by model - a model with a TenantCondition
    policy saw no row."""
    skip_if_not_postgres()
    async with tenant_context() as (_, role):
        with Tenancy.scope({RlsNote: Tenancy.any_of(1, 2), RlsLabel: "acme"}):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                assert await count_rows(connection, "rls_note") == 2
                assert await count_rows(connection, "rls_label") == 1
                assert sorted(obj.text for obj in await RlsNote.objects.all()) == ["one", "two"]
                assert [obj.id for obj in await RlsLabel.objects.all()] == [1]
                await RlsLabel.objects.create(id=3)
                with pytest.raises(OperationalError, match="row-level security"):
                    async with Transactions.atomic("models") as savepoint:
                        await savepoint.execute("INSERT INTO rls_label VALUES (4, 'beta')")
                with pytest.raises(OperationalError, match="row-level security"):
                    async with Transactions.atomic("models") as savepoint:
                        await savepoint.execute("INSERT INTO rls_note VALUES (5, 3, 'five')")
        with Tenancy.scope({RlsNote: Tenancy.ALL, RlsLabel: "beta"}):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                assert await count_rows(connection, "rls_note") == 3
                assert await count_rows(connection, "rls_label") == 1


@pytest.mark.parametrize("subject_to_the_policy", [True, False])
@pytest.mark.asyncio
async def test_a_relation_to_a_row_the_policy_hides_is_refused(subject_to_the_policy):
    """The policy hid the other tenant's row from the check, and the database's foreign key took it."""
    skip_if_not_postgres()
    async with tenant_context() as (_, role):
        with Tenancy.scope({RlsNote: Tenancy.any_of(1, 2)}):
            async with Transactions.atomic("models") as connection:
                if subject_to_the_policy:
                    await as_role(connection, role)
                await RlsMention.objects.create(id=1, note_id=1)
                await RlsMention.objects.bulk_create([RlsMention(id=2, note_id=2), RlsMention(id=3, note_id=1)])
                for write in (
                    lambda: RlsMention.objects.create(id=4, note_id=3),
                    lambda: RlsMention.objects.bulk_create([RlsMention(id=4, note_id=1), RlsMention(id=5, note_id=3)]),
                    lambda: RlsMention.objects.filter(id=1).update(note_id=3),
                ):
                    with pytest.raises(QueryError, match="can't cross tenants"):
                        async with Transactions.atomic("models"):
                            await write()
                assert await count_rows(connection, "rls_mention") == 3


@pytest.mark.asyncio
async def test_a_table_the_scope_given_model_by_model_leaves_out_sees_no_row():
    skip_if_not_postgres()
    async with tenant_context() as (_, role):
        with Tenancy.scope({RlsNote: 1}):
            async with Transactions.atomic("models") as connection:
                await as_role(connection, role)
                assert await count_rows(connection, "rls_note") == 1
                assert await count_rows(connection, "rls_label") == 0
                with pytest.raises(QueryError, match="doesn't name RlsLabel"):
                    await RlsLabel.objects.count()
                with Tenancy.scope({RlsNote: 2}), pytest.raises(QueryError, match="began in"):
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


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_tenant_condition_needs_a_tenant_field():
    with pytest.raises(ConfigurationError, match="Meta.tenant_field"):
        async with hare_test_context(
            modules=["tests.dialects.postgresql.tenancy.models_tenant_row_level_security_no_field"],
            app_label="models",
            connection_label="models",
        ):
            pass


@pytest.mark.database_independent
def test_the_policy_and_the_setting_sql():
    dialect: Any = DialectRegistry.get_dialect("postgresql")
    condition_sql = dialect.schema_editor_class.tenant_conditions_class.get_tenant_condition_sql(
        '"company_id"', "INT", "'shop.rls_note'"
    )
    tenants_sql = (
        "(SELECT CASE jsonb_typeof(hare_tenants.setting) WHEN 'object' THEN hare_tenants.setting -> "
        "'shop.rls_note' ELSE hare_tenants.setting END FROM (SELECT NULLIF(current_setting('hare.tenant', "
        "true), '')::jsonb AS setting) AS hare_tenants)"
    )
    assert condition_sql == (
        f"CASE current_setting('hare.tenant', true) WHEN '*' THEN true ELSE CASE {tenants_sql} WHEN "
        f'\'"*"\'::jsonb THEN true ELSE "company_id" = ANY (ARRAY(SELECT jsonb_array_elements_text('
        f"{tenants_sql}))::INT[]) END END"
    )
    assert dialect.transactions.get_table_tenants_setting_sql({"rls_note": ["1", "2"], "shop.faq": None}) == (
        'SELECT set_config(\'hare.tenant\', \'{"rls_note": ["1", "2"], "shop.faq": "*"}\', true)'
    )
    assert (
        dialect.transactions.get_tenant_setting_sql(["7", "o'brien"])
        == "SELECT set_config('hare.tenant', '[\"7\", \"o''brien\"]', true)"
    )
    assert dialect.transactions.get_tenant_setting_sql(None) == "SELECT set_config('hare.tenant', '*', true)"
    sqlite_dialect: Any = DialectRegistry.get_dialect("sqlite")
    with pytest.raises(Exception, match="row level security"):
        sqlite_dialect.transactions.get_tenant_setting_sql(["7"])


@pytest.mark.asyncio
async def test_a_scope_given_model_by_model_entered_again_in_its_transaction_is_the_same_scope():
    """Each Tenancy.scope({...}) made a scope of its own - entering one of the same entries inside the
    transaction refused every query as a switch to another scope."""
    skip_if_not_postgres()
    async with tenant_context():
        with Tenancy.scope({RlsNote: 1}):
            async with Transactions.atomic("models"):
                with Tenancy.scope({RlsNote: 1}):
                    assert await RlsNote.objects.count() == 1
                with Tenancy.scope({RlsNote: 2}), pytest.raises(QueryError, match="began in"):
                    await RlsNote.objects.count()
        with Tenancy.scope({RlsNote: Tenancy.any_of(1, 2)}):
            async with Transactions.atomic("models"):
                with Tenancy.scope({RlsNote: Tenancy.any_of(2, 1)}):
                    assert await RlsNote.objects.count() == 2
