"""A scope given model by model on models with a ``TenantCondition`` policy that are versioned, keyed by a
composite key, soft-deleted and captured into an outbox: the database keeps each table to the tenants the
scope gives its model."""

from __future__ import annotations

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.tenancy.models_tenant_row_level_security_combinations import (
    RlsDocument,
    RlsEvent,
    RlsLine,
)

MODULE = "tests.dialects.postgresql.tenancy.models_tenant_row_level_security_combinations"


def tenant_context():
    db_url = os.environ["HARE_TEST_DB"]
    return hare_test_context(
        modules=[MODULE],
        db_url=f"{db_url}{'&' if '?' in db_url else '?'}tenant_row_level_security=true",
        app_label="models",
        connection_label="models",
        reuse_databases=False,
    )


async def count_rows(connection, table: str) -> int:
    _, rows = await connection.execute(f"SELECT count(*) AS row_count FROM {table}")
    return rows[0]["row_count"]


@pytest.mark.asyncio
async def test_versions_composite_keys_soft_deletes_and_captured_changes_keep_to_their_tenants():
    skip_if_not_postgres()
    async with tenant_context():
        connection = Connections.get("models")
        _, rows = await connection.execute("SELECT current_database() AS name")
        role = f"hare_rls_{rows[0]['name']}"[:63]
        await connection.execute_script(
            f'DROP ROLE IF EXISTS "{role}"; CREATE ROLE "{role}" NOLOGIN; '
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON rls_document, rls_line, rls_event TO "{role}"; '
            f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO "{role}"'
        )
        try:
            with Tenancy.scope(Tenancy.ALL):
                async with Transactions.atomic("models"):
                    other = await RlsDocument.objects.create(company_id=2, title="other")
                    await RlsLine.objects.create(order_id=9, number=1, company_id=2)
            with Tenancy.scope({RlsDocument: 1, RlsLine: Tenancy.any_of(1, 3)}):
                async with Transactions.atomic("models") as transaction:
                    await transaction.execute(f'SET LOCAL ROLE "{role}"')
                    document = await RlsDocument.objects.create(company_id=1, title="first")
                    await document.create_new_version(title="second")
                    latest = await RlsDocument.get_last_version_or_exception(id=document.id)
                    assert latest.title == "second"
                    assert await count_rows(transaction, "rls_document") == 2
                    assert not await RlsDocument.objects.filter(id=other.id).exists()
                    await RlsLine.objects.bulk_create(
                        [RlsLine(order_id=1, number=1, company_id=1), RlsLine(order_id=1, number=2, company_id=3)]
                    )
                    await (await RlsLine.objects.get(order_id=1, number=1)).delete()
                    assert await RlsLine.objects.values_list("number", flat=True) == [2]
                    assert await RlsLine.objects.include_deleted().count() == 2
                    assert await count_rows(transaction, "rls_line") == 2
                    # The two writes of the other tenant, then five here: a document, its version, two lines, a
                    # soft delete.
                    assert await RlsEvent.objects.count() == 7
        finally:
            await connection.execute_script(f'DROP OWNED BY "{role}"; DROP ROLE "{role}"')
