"""Changes captured into an outbox kept in each tenant's schema: written to the active tenant's schema, and
refused with hare's QueryError outside a single-tenant scope - before any SQL, as the outbox's own queries
are."""

from __future__ import annotations

import os

import pytest

from hare.contrib.test import hare_test_context
from hare.exceptions import QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.tenancy.models_tenant_schema_outbox import SharedCapturedRow, TenantSchemaEvent
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
async def test_a_change_captured_into_a_tenant_schema_outbox_needs_a_tenant_scope():
    skip_if_not_postgres()
    await DatabaseUnderTest.skip_if_the_pooler_loses_the_search_path()
    db_url = os.environ["HARE_TEST_DB"]
    async with hare_test_context(
        modules=["tests.dialects.postgresql.tenancy.models_tenant_schema_outbox"],
        db_url=f"{db_url}{'&' if '?' in db_url else '?'}tenant_schema_template=tenant_%7Btenant%7D",
        app_label="models",
        connection_label="models",
        reuse_databases=False,
    ):
        await TenantSchemas.create("acme", "models")
        with Tenancy.scope("acme"):
            await SharedCapturedRow.objects.create(id=1)
            assert await TenantSchemaEvent.objects.count() == 1

        with pytest.raises(QueryError, match="Meta.tenant_schema"):
            await SharedCapturedRow.objects.create(id=2)
        assert await SharedCapturedRow.objects.values_list("id", flat=True) == [1]
