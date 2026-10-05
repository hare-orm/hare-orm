"""A schema per tenant: ``Meta.tenant_schema`` models keep their tables in each tenant's schema
(``tenant_schema_template``), reached inside a single-tenant ``Tenancy.scope()``; shared models stay in
the connection's own schema; ``migrate`` brings the shared schema and every tenant schema up to date."""

from __future__ import annotations

import importlib
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.exceptions import ConfigurationError, QueryError, UnSupportedError, ValidationError
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.autodetection.diffs.state_model_diff import StateModelDiff
from hare.migrations.execution.executor.migration_executor import MigrationExecutor
from hare.migrations.execution.executor.migration_target import MigrationTarget
from hare.migrations.state.model_state import ModelState
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas
from hare.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.models_tenant_schemas import TenantCompany, TenantNote

MODULE = "tests.dialects.postgresql.models_tenant_schemas"
TEMPLATE_QUERY = "tenant_schema_template=tenant_%7Btenant%7D"


def get_db_url() -> str:
    db_url = os.environ["HARE_TEST_DB"]
    return f"{db_url}{'&' if '?' in db_url else '?'}{TEMPLATE_QUERY}"


def tenant_context(**kwargs: Any):
    return hare_test_context(
        modules=[MODULE],
        db_url=get_db_url(),
        app_label="models",
        connection_label="models",
        reuse_databases=False,
        **kwargs,
    )


async def get_tables(schema: str) -> set[str]:
    return set(await DatabaseCatalog.get_table_names(Connections.get("models"), schema=schema))


@pytest.mark.asyncio
async def test_each_tenant_has_its_own_tables_and_shares_the_others():
    skip_if_not_postgres()
    async with tenant_context():
        assert await TenantSchemas.create("acme", "models") == "tenant_acme"
        assert await TenantSchemas.create(7, "models") == "tenant_7"
        assert await TenantSchemas.get_tenants("models") == ["7", "acme"]
        assert "tenant_note" not in await get_tables("public")
        assert {"tenant_note"} <= await get_tables("tenant_acme")
        assert "tenant_company" not in await get_tables("tenant_acme")

        company = await TenantCompany.objects.create(id=1, name="shared")
        with Tenancy.scope("acme"):
            await TenantNote.objects.create(id=1, text="acme note", company=company)
            assert await TenantCompany.objects.count() == 1
            note = await TenantNote.objects.select_related("company").get(id=1)
            assert note.company.name == "shared"
        with Tenancy.scope(7):
            assert await TenantNote.objects.count() == 0
            await TenantNote.objects.create(id=1, text="seven note")
            assert [obj.text for obj in await TenantNote.objects.all()] == ["seven note"]
        with Tenancy.scope("acme"):
            assert [obj.text for obj in await TenantNote.objects.all()] == ["acme note"]
            assert [obj.text for obj in await company.notes.all()] == ["acme note"]

        with pytest.raises(QueryError, match="single tenant"):
            await TenantNote.objects.count()
        with Tenancy.scope(Tenancy.any_of("acme", 7)), pytest.raises(QueryError, match="single tenant"):
            await TenantNote.objects.count()
        with Tenancy.scope(Tenancy.ALL), pytest.raises(QueryError, match="single tenant"):
            await TenantNote.objects.count()

        await TenantSchemas.drop("acme", "models")
        assert await TenantSchemas.get_tenants("models") == ["7"]


@pytest.mark.asyncio
async def test_a_transaction_stays_in_the_schema_it_began_in():
    skip_if_not_postgres()
    async with tenant_context():
        await TenantSchemas.create("acme", "models")
        await TenantSchemas.create("beta", "models")
        with Tenancy.scope("acme"):
            async with Transactions.atomic("models"):
                await TenantNote.objects.create(id=1, text="in transaction")
                with Tenancy.scope("beta"), pytest.raises(QueryError, match="tenant_acme"):
                    await TenantNote.objects.count()
                with Tenancy.scope(None), pytest.raises(QueryError, match="tenant_acme"):
                    await TenantCompany.objects.count()
            assert await TenantNote.objects.count() == 1
        async with Transactions.atomic("models"):
            await TenantCompany.objects.create(id=1, name="shared")
            with Tenancy.scope("acme"), pytest.raises(QueryError, match="of no tenant"):
                await TenantNote.objects.count()
        with Tenancy.scope("acme"):
            with pytest.raises(RuntimeError, match="rolled back"):
                async with Transactions.atomic("models"):
                    await TenantNote.objects.create(id=2, text="rolled back")
                    raise RuntimeError("rolled back")
            assert await TenantNote.objects.count() == 1
            async with Transactions.autonomous("models") as connection:
                assert connection.tenant_schema == "tenant_acme"
                _, rows = await connection.execute("SELECT count(*) AS notes FROM tenant_note")
                assert rows[0]["notes"] == 1


@pytest.mark.asyncio
async def test_a_tenant_value_must_name_a_schema():
    skip_if_not_postgres()
    async with tenant_context():
        for tenant in ("Acme", "acme-1", "", uuid.UUID(int=1), 1.5, "x" * 60):
            with pytest.raises(ValidationError):
                await TenantSchemas.create(tenant, "models")
            with Tenancy.scope(tenant), pytest.raises(ValidationError):
                await TenantCompany.objects.count()
        assert await TenantSchemas.create(uuid.UUID(int=1).hex, "models") == f"tenant_{uuid.UUID(int=1).hex}"


@pytest.mark.asyncio
async def test_tenant_schemas_are_managed_outside_a_scope_and_a_transaction():
    skip_if_not_postgres()
    async with tenant_context():
        with Tenancy.scope("acme"), pytest.raises(QueryError, match="outside a tenant scope"):
            await TenantSchemas.create("beta", "models")
        async with Transactions.atomic("models"):
            with pytest.raises(QueryError, match="outside a tenant scope"):
                await TenantSchemas.drop("beta", "models")
        assert await TenantSchemas.get_tenants("models") == []


@pytest.mark.parametrize("template", ["tenant", "{tenant}_{tenant}", "Tenant_{tenant}", "tenant-{tenant}", 5])
def test_a_template_takes_one_placeholder_and_plain_characters(template):
    with pytest.raises(ConfigurationError, match="tenant_schema_template"):
        TenantSchemas.get_checked_template(template)


def test_the_template_maps_tenants_to_schemas_and_back():
    assert TenantSchemas.get_checked_template("t_{tenant}_data") == "t_{tenant}_data"

    class Client:
        tenant_schema_template = "t_{tenant}_data"

    client: Any = Client()
    assert TenantSchemas.get_tenant(client, "t_acme_data") == "acme"
    assert TenantSchemas.get_tenant(client, "t__data") is None
    assert TenantSchemas.get_tenant(client, "public") is None
    assert TenantSchemas.get_tenant(client, "t_Acme_data") is None


@pytest.mark.asyncio
async def test_a_connection_without_a_template_refuses_tenant_models():
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(
        modules=[MODULE], db_url=db_url, app_label="models", connection_label="models", _generate_schemas=False
    ) as ctx:
        with pytest.raises(ConfigurationError, match="no tenant_schema_template"):
            await ctx.generate_schemas()
        with pytest.raises(ConfigurationError, match="no tenant_schema_template"):
            await TenantNote.objects.count()
        if not ctx.get_connection().features.supports_tenant_schemas:
            with pytest.raises(UnSupportedError, match="schema per tenant"):
                await TenantSchemas.create("acme", "models")
        else:
            with pytest.raises(ConfigurationError, match="no tenant_schema_template"):
                await TenantSchemas.create("acme", "models")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "message"),
    [
        ("models_tenant_schemas_shared_relation", "each tenant's schema"),
        ("models_tenant_schemas_with_schema", "Meta.schema"),
        ("models_tenant_schemas_not_a_flag", "True or False"),
    ],
)
async def test_tenant_schema_declarations_are_checked(module, message):
    with pytest.raises(ConfigurationError, match=message):
        async with hare_test_context(
            modules=[f"tests.dialects.postgresql.{module}"], app_label="models", connection_label="models"
        ):
            pass


def test_migrations_carry_the_flag_and_refuse_changing_it():
    model_state = ModelState.make_from_model("models", TenantNote)
    assert model_state.options["tenant_schema"] is True
    assert "tenant_schema" not in ModelState.make_from_model("models", TenantCompany).options
    new_state = ModelState.make_from_model("models", TenantNote)
    new_state.options.pop("tenant_schema")
    with pytest.raises(ConfigurationError, match="tenant_schema"):
        StateModelDiff(model_state, new_state).generate_operations()


INITIAL_MIGRATION = """
from hare import fields, migrations
from hare.migrations import operations as ops


async def seed_notes(apps, schema_editor):
    note_model = apps.get_model("models", "TenantNote")
    await note_model.objects.create(id=1, text="seeded")


class Migration(migrations.Migration):
    operations = [
        ops.CreateModel(
            name="TenantCompany",
            fields=[("id", fields.IntField(primary_key=True)), ("name", fields.CharField(max_length=50))],
            options={"table": "tenant_company"},
        ),
        ops.CreateModel(
            name="TenantNote",
            fields=[("id", fields.IntField(primary_key=True)), ("text", fields.CharField(max_length=50))],
            options={"table": "tenant_note", "tenant_schema": True},
        ),
        ops.RunSQL("CREATE TABLE shared_marker (id integer)", "DROP TABLE shared_marker"),
        ops.RunSQL("CREATE TABLE tenant_marker (id integer)", "DROP TABLE tenant_marker", tenant_schema=True),
        ops.RunPython(seed_notes, migrations.RunPython.noop, tenant_schema=True),
    ]
"""
ADD_SIZE_MIGRATION = """
from hare import fields, migrations
from hare.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("models", "0001_initial")]
    operations = [ops.AddField(model_name="TenantNote", name="size", field=fields.IntField(default=0))]
"""


def write_migrations(tmp_path: Path) -> str:
    package_name = f"tenant_schema_migrations_{tmp_path.name}"
    migrations_path = tmp_path / package_name
    migrations_path.mkdir(parents=True)
    (migrations_path / "__init__.py").write_text("", encoding="utf-8")
    (migrations_path / "0001_initial.py").write_text(INITIAL_MIGRATION.lstrip(), encoding="utf-8")
    (migrations_path / "0002_add_size.py").write_text(ADD_SIZE_MIGRATION.lstrip(), encoding="utf-8")
    importlib.invalidate_caches()
    return package_name


async def get_columns(schema: str, table: str) -> list[str]:
    (table_info,) = await DatabaseCatalog.inspect_tables(Connections.get("models"), [table], schema=schema)
    return [column.name for column in table_info.columns]


async def get_journal(schema: str) -> list[str]:
    _, rows = await Connections.get("models").execute(f"SELECT name FROM {schema}.hare_migrations ORDER BY name")
    return [row["name"] for row in rows]


@pytest.mark.asyncio
async def test_migrate_runs_in_the_shared_schema_and_every_tenant_schema(tmp_path, monkeypatch):
    skip_if_not_postgres()
    monkeypatch.syspath_prepend(str(tmp_path))
    migrations_module = write_migrations(tmp_path)
    apps_config = {"models": {"models": [MODULE], "default_connection": "models", "migrations": migrations_module}}
    async with tenant_context(_generate_schemas=False):
        await TenantSchemas.create("acme", "models", create_tables=False)
        await TenantSchemas.create("beta", "models", create_tables=False)

        def get_executor() -> MigrationExecutor:
            return MigrationExecutor(Connections.get("models"), apps_config)

        await get_executor().migrate([MigrationTarget(app_label="models", name="0001_initial")])
        assert {"tenant_company", "shared_marker", "hare_migrations"} <= await get_tables("public")
        assert not {"tenant_note", "tenant_marker"} & await get_tables("public")
        for schema in ("tenant_acme", "tenant_beta"):
            assert {"tenant_note", "tenant_marker", "hare_migrations"} <= await get_tables(schema)
            assert not {"tenant_company", "shared_marker"} & await get_tables(schema)
            assert await get_journal(schema) == ["0001_initial"]
        for schema in ("tenant_acme", "tenant_beta"):
            _, rows = await Connections.get("models").execute(f"SELECT text FROM {schema}.tenant_note")
            assert [row["text"] for row in rows] == ["seeded"]

        # A tenant added later gets every migration; the others get the new one.
        await TenantSchemas.create("gamma", "models", create_tables=False)
        await get_executor().migrate()
        for schema in ("tenant_acme", "tenant_beta", "tenant_gamma"):
            assert "size" in await get_columns(schema, "tenant_note")
            assert await get_journal(schema) == ["0001_initial", "0002_add_size"]
        assert await get_journal("public") == ["0001_initial", "0002_add_size"]

        # Inside a tenant scope only that tenant's schema is migrated.
        with Tenancy.scope("acme"):
            await get_executor().migrate([MigrationTarget(app_label="models", name="0001_initial")])
        assert "size" not in await get_columns("tenant_acme", "tenant_note")
        assert "size" in await get_columns("tenant_beta", "tenant_note")
        assert await get_journal("public") == ["0001_initial", "0002_add_size"]

        await get_executor().migrate([MigrationTarget(app_label="models", name="zero")])
        assert not {"tenant_company", "shared_marker"} & await get_tables("public")
        for schema in ("tenant_acme", "tenant_beta", "tenant_gamma"):
            assert not {"tenant_note", "tenant_marker"} & await get_tables(schema)
            assert await get_journal(schema) == []
