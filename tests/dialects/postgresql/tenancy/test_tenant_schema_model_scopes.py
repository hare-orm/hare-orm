"""A scope given model by model with ``Meta.tenant_schema`` models: the connection works in the schema of the
one tenant the scope gives its tenant schema models, a model divided by a tenant column beside them is
limited by its own entry, and the shared tables are reached as in any scope."""

from __future__ import annotations

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.exceptions import QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas
from hare.query.expressions import Exists, OuterReference
from hare.query.scopes.tenants.model_tenants import ModelTenants
from hare.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.tenancy.models_tenant_schema_model_scopes import (
    ScopeDocument,
    ScopeEvent,
    ScopeLine,
    ScopeMemo,
    ScopeNote,
    ScopeOrder,
    ScopeShop,
    ScopeTask,
    ScopeTenantBase,
)
from tests.utils.database_under_test import DatabaseUnderTest

MODULE = "tests.dialects.postgresql.tenancy.models_tenant_schema_model_scopes"
TEMPLATE_QUERY = "tenant_schema_template=tenant_%7Btenant%7D"


def tenant_context():
    skip_if_not_postgres()
    db_url = os.environ["HARE_TEST_DB"]
    return hare_test_context(
        modules=[MODULE],
        db_url=f"{db_url}{'&' if '?' in db_url else '?'}{TEMPLATE_QUERY}",
        app_label="models",
        connection_label="models",
        reuse_databases=False,
    )


async def create_tenants() -> None:
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    await TenantSchemas.create("acme", "models")
    await TenantSchemas.create("beta", "models")
    await ScopeShop.objects.create(id=1, name="shared")
    await ScopeOrder.objects.all_tenants().bulk_create([ScopeOrder(id=1, company=1), ScopeOrder(id=2, company=2)])


@pytest.mark.asyncio
async def test_scopes_recheck_schema_tenants_after_live_models_change():
    async with tenant_context() as context:
        context.unregister_live_models([ScopeTask])
        scopes = [ModelTenants({ScopeNote: "acme", ScopeTask: "beta"}) for _ in range(2)]
        for scope in scopes:
            assert scope.get_schema_tenant("models") == "acme"
        context.register_live_models([ScopeTask], app_label="models", connection_alias="models")
        for scope in scopes:
            with pytest.raises(QueryError, match="different tenants"):
                scope.get_schema_tenant("models")
        context.unregister_live_models([ScopeTask, ScopeNote])
        for scope in scopes:
            assert scope.get_schema_tenant("models") is None


@pytest.mark.asyncio
async def test_queries_run_in_the_schema_the_scope_gives_the_tenant_schema_models():
    async with tenant_context():
        await create_tenants()
        shop = await ScopeShop.objects.get(id=1)
        with Tenancy.scope({ScopeNote: "acme", ScopeTask: "acme", ScopeOrder: 1}):
            assert Tenancy.get_scope(ScopeNote) == "acme"
            note = await ScopeNote.objects.create(id=1, text="acme note", shop=shop)
            await ScopeNote.objects.bulk_create([ScopeNote(id=2, text="second"), ScopeNote(id=3, text="third")])
            await ScopeTask.objects.create(id=1, note=note)
            await ScopeNote.objects.filter(id=2).update(text="updated")
            await ScopeNote.objects.filter(id=3).delete()
            assert await ScopeOrder.objects.values_list("id", flat=True) == [1]
            loaded = await ScopeNote.objects.select_related("shop").get(id=1)
            assert loaded.shop.name == "shared"
            assert [task.id for task in (await ScopeNote.objects.prefetch_related("tasks").get(id=1)).tasks] == [1]
            with_tasks = ScopeNote.objects.filter(Exists(ScopeTask.objects.filter(note_id=OuterReference("id"))))
            assert await with_tasks.values_list("id", flat=True) == [1]
            assert sorted(await ScopeNote.objects.values_list("text", flat=True)) == ["acme note", "updated"]
            assert [obj.text for obj in await shop.notes.all()] == ["acme note"]
        with Tenancy.scope({ScopeNote: "beta"}):
            assert await ScopeNote.objects.count() == 0
        with Tenancy.scope("acme"):
            assert await ScopeNote.objects.count() == 2
            assert await ScopeNote.objects.include_deleted().count() == 3


@pytest.mark.asyncio
async def test_a_base_class_entry_is_its_tenant_schema_subclasses():
    async with tenant_context():
        await create_tenants()
        with Tenancy.scope({ScopeTenantBase: "beta"}):
            assert Tenancy.get_scope(ScopeMemo) == "beta"
            await ScopeMemo.objects.create(id=1, text="beta memo")
        with Tenancy.scope("beta"):
            assert await ScopeMemo.objects.values_list("text", flat=True) == ["beta memo"]


@pytest.mark.parametrize(("note_tenant", "task_tenant"), [(1, "1"), ("1", 1)])
@pytest.mark.asyncio
async def test_integer_and_text_values_naming_one_schema_are_the_same_connection_tenant(note_tenant, task_tenant):
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    async with tenant_context():
        await TenantSchemas.create(1, "models")
        with Tenancy.scope({ScopeNote: note_tenant, ScopeTask: task_tenant}):
            note = await ScopeNote.objects.create(id=1, text="same schema")
            await ScopeTask.objects.create(id=1, note=note)
            assert await ScopeNote.objects.count() == 1
            assert await ScopeTask.objects.count() == 1
            async with Transactions.atomic("models") as connection:
                assert connection.tenant_schema == "tenant_1"


@pytest.mark.asyncio
async def test_a_tenant_schema_model_takes_one_tenant():
    async with tenant_context():
        await create_tenants()
        for scope in (Tenancy.any_of("acme", "beta"), Tenancy.ALL):
            with pytest.raises(QueryError, match="ScopeNote has Meta.tenant_schema"):
                with Tenancy.scope({ScopeNote: scope}):
                    pass
        with Tenancy.scope({ScopeNote: Tenancy.any_of("acme")}):
            assert Tenancy.get_scope(ScopeNote) == "acme"
        with Tenancy.scope({ScopeNote: "acme", ScopeTask: "beta"}):
            with pytest.raises(QueryError, match="different tenants"):
                await ScopeNote.objects.count()
        with Tenancy.scope({ScopeOrder: 2}):
            # No tenant schema model named - only the shared tables are reached.
            assert await ScopeOrder.objects.values_list("id", flat=True) == [2]
            with pytest.raises(QueryError, match="ScopeNote"):
                await ScopeNote.objects.count()


@pytest.mark.asyncio
async def test_a_transaction_runs_in_the_schema_of_the_scope_it_began_in():
    async with tenant_context():
        await create_tenants()
        with Tenancy.scope({ScopeNote: "acme", ScopeOrder: Tenancy.ALL}):
            async with Transactions.atomic("models") as connection:
                assert connection.tenant_schema == "tenant_acme"
                await ScopeNote.objects.create(id=1, text="in transaction")
                assert await ScopeOrder.objects.count() == 2
                with Tenancy.scope({ScopeNote: "beta"}), pytest.raises(QueryError, match="tenant_acme"):
                    await ScopeNote.objects.count()
            async with Transactions.autonomous("models") as connection:
                assert connection.tenant_schema == "tenant_acme"
                _, rows = await connection.execute("SELECT count(*) AS notes FROM scope_note")
                assert rows[0]["notes"] == 1
        with Tenancy.scope("acme"):
            assert await ScopeNote.objects.count() == 1


@pytest.mark.asyncio
async def test_versions_composite_keys_and_captured_changes_in_a_scope_given_model_by_model():
    async with tenant_context():
        await create_tenants()
        scope = {ScopeDocument: "beta", ScopeLine: "beta", ScopeEvent: "beta"}
        with Tenancy.scope(scope):
            document = await ScopeDocument.objects.create(title="first")
            second = await document.create_new_version(title="second")
            latest = await ScopeDocument.get_last_version_or_exception(id=document.id)
            assert (latest.version, latest.title) == (second.version, "second")
            await ScopeLine.objects.bulk_create([ScopeLine(order_id=1, number=1), ScopeLine(order_id=1, number=2)])
            await ScopeLine.objects.filter(order_id=1, number=2).update(amount=5)
            await (await ScopeLine.objects.get(order_id=1, number=1)).delete()
            assert await ScopeLine.objects.values_list("number", "amount") == [(2, 5)]
            assert await ScopeEvent.objects.count() == 6
        with Tenancy.scope("acme"):
            assert await ScopeDocument.objects.count() == 0
            assert await ScopeEvent.objects.count() == 0
        with Tenancy.scope({ScopeDocument: "beta", ScopeEvent: "acme"}):
            with pytest.raises(QueryError, match="different tenants"):
                await ScopeDocument.objects.create(title="split")
