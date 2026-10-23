"""A row every tenant shares, deleted or restored: its ``on_delete`` reaches the rows of
``Meta.tenant_schema`` models in every tenant's schema - not only in the one the delete runs in."""

from __future__ import annotations

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.exceptions import ProtectedError
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.tenancy.models_tenant_schemas_shared_deletes import (
    SharedAccount,
    SharedClient,
    SharedLedger,
    TenantBill,
    TenantEntry,
    TenantGuard,
    TenantNoteBook,
)
from tests.utils.database_under_test import DatabaseUnderTest

MODULE = "tests.dialects.postgresql.tenancy.models_tenant_schemas_shared_deletes"
TEMPLATE_QUERY = "tenant_schema_template=tenant_%7Btenant%7D"


def tenant_context():
    db_url = os.environ["HARE_TEST_DB"]
    return hare_test_context(
        modules=[MODULE],
        db_url=f"{db_url}{'&' if '?' in db_url else '?'}{TEMPLATE_QUERY}",
        app_label="models",
        connection_label="models",
        reuse_databases=False,
    )


async def create_tenants_with_bills() -> None:
    await TenantSchemas.create("acme", "models")
    await TenantSchemas.create("beta", "models")
    await SharedClient.objects.bulk_create([SharedClient(id=1, name="one"), SharedClient(id=2, name="two")])
    for tenant in ("acme", "beta"):
        with Tenancy.scope(tenant):
            await TenantBill.objects.bulk_create([TenantBill(id=1, client_id=1), TenantBill(id=2, client_id=2)])


async def get_live_bill_ids(tenant: str) -> list[int]:
    with Tenancy.scope(tenant):
        return await TenantBill.objects.order_by("id").values_list("id", flat=True)


@pytest.mark.asyncio
async def test_a_shared_row_deleted_in_one_tenant_cascades_in_every_tenant_schema():
    """The cascade ran in the deleting tenant's schema alone - beta's bills kept pointing at a
    soft-deleted client."""
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    async with tenant_context():
        await create_tenants_with_bills()
        with Tenancy.scope("acme"):
            await (await SharedClient.objects.get(id=1)).delete()
        assert await get_live_bill_ids("acme") == [2]
        assert await get_live_bill_ids("beta") == [2]

        with Tenancy.scope("beta"):
            await (await SharedClient.objects.include_deleted().get(id=1)).restore(cascade=True)
        assert await get_live_bill_ids("acme") == [1, 2]
        assert await get_live_bill_ids("beta") == [1, 2]


@pytest.mark.asyncio
async def test_a_shared_row_is_deleted_outside_any_tenant_scope():
    """A delete outside a tenant scope failed - the cascade reached a tenant schema's table."""
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    async with tenant_context():
        await create_tenants_with_bills()
        await (await SharedClient.objects.get(id=1)).delete()
        await SharedClient.objects.filter(id=2).delete()
        assert await get_live_bill_ids("acme") == []
        assert await get_live_bill_ids("beta") == []


@pytest.mark.asyncio
async def test_a_protecting_row_of_another_tenant_schema_blocks_the_delete():
    """A PROTECT row in another tenant's schema was not looked at: the soft delete went through and a
    hard delete failed with the database's own foreign key error."""
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    async with tenant_context():
        await create_tenants_with_bills()
        with Tenancy.scope("beta"):
            await TenantGuard.objects.create(id=1, client_id=1)
        with Tenancy.scope("acme"):
            client = await SharedClient.objects.get(id=1)
            assert len((await client.delete_preview()).protected_by) == 1
            with pytest.raises(ProtectedError):
                await client.delete()
            with pytest.raises(ProtectedError):
                await client.hard_delete()
            assert await SharedClient.objects.filter(id=1).count() == 1
        assert await get_live_bill_ids("acme") == [1, 2]
        assert await get_live_bill_ids("beta") == [1, 2]


@pytest.mark.asyncio
async def test_a_shared_row_is_hard_deleted_outside_any_tenant_scope():
    """A hard delete outside a tenant scope raised QueryError - reading whether the database enforces
    a relation took the client of a tenant model, which needs a tenant."""
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    async with tenant_context():
        await TenantSchemas.create("acme", "models")
        await SharedAccount.objects.create(id=1)
        await SharedLedger.objects.create(id=1, account_id=1)
        with Tenancy.scope("acme"):
            book = await TenantNoteBook.objects.create(id=1)
            await book.accounts.add(await SharedAccount.objects.get(id=1))
        preview = await (await SharedAccount.objects.get(id=1)).delete_preview()
        assert preview.many_to_many_through == {"tenant_note_book_shared_account": 1}
        await (await SharedLedger.objects.get(id=1)).hard_delete()
        await SharedAccount.objects.filter(id=1).delete()
        with Tenancy.scope("acme"):
            assert await (await TenantNoteBook.objects.get(id=1)).accounts.count() == 0


@pytest.mark.asyncio
async def test_a_protecting_row_the_cascade_removes_in_another_schema_does_not_block_it():
    """The PROTECT constraint of the other tenant's schema wasn't deferred - its row, removed by the
    same cascade, failed the delete with the database's foreign key error."""
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    async with tenant_context():
        await TenantSchemas.create("acme", "models")
        await TenantSchemas.create("beta", "models")
        await SharedAccount.objects.create(id=1)
        await SharedLedger.objects.create(id=1, account_id=1)
        with Tenancy.scope("beta"):
            await TenantEntry.objects.create(id=1, ledger_id=1, account_id=1)
        for tenant in ("acme", "beta"):
            with Tenancy.scope(tenant):
                await (await SharedAccount.objects.get(id=1)).hard_delete()
                assert await SharedAccount.objects.count() == 0
                await SharedAccount.objects.create(id=1)
                await SharedLedger.objects.create(id=1, account_id=1)
            with Tenancy.scope("beta"):
                await TenantEntry.objects.create(id=1, ledger_id=1, account_id=1)


@pytest.mark.asyncio
async def test_a_tenant_schema_without_the_tables_is_passed_over():
    """A schema named like a tenant's but made before the models' tables failed every delete with
    "relation does not exist"."""
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    async with tenant_context():
        await create_tenants_with_bills()
        await TenantSchemas.create("empty", "models", create_tables=False)
        with Tenancy.scope("acme"):
            client = await SharedClient.objects.get(id=1)
            assert (await client.delete_preview()).soft_deleted
            await client.delete()
        assert await get_live_bill_ids("beta") == [2]
