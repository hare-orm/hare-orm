"""A direct get() of a model with Meta.soft_delete_field/Meta.tenant_field runs on its plan with the
default scope's filters in it - the tenant bound is the one active when the query runs."""

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import DoesNotExist, QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.plans.call_signatures.call_signature_plans import CallSignaturePlans
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from tests import testmodels


def get_direct_plan(query):
    signature_key = CallSignaturePlans.get_key(ModelRowsQuery, query, query.get_connection(), ())
    return None if signature_key is None else CallSignaturePlans.find(query.model, signature_key[0])


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_soft_deleted_rows_stay_hidden_on_the_plan(db):
    kept = await testmodels.SoftDeleteStandalone.objects.create(name="kept")
    removed = await testmodels.SoftDeleteStandalone.objects.create(name="removed")
    await removed.delete()
    assert (await testmodels.SoftDeleteStandalone.objects.get(pk=kept.pk)).name == "kept"
    query = testmodels.SoftDeleteStandalone.objects.get(pk=removed.pk)
    assert get_direct_plan(query) is not None
    with pytest.raises(DoesNotExist):
        await query
    for _ in range(2):
        shown = await testmodels.SoftDeleteStandalone.objects.include_deleted().get(pk=removed.pk)
        assert shown.name == "removed"
        assert await testmodels.SoftDeleteStandalone.objects.get(does_not_exist_exception=None, pk=removed.pk) is None


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_tenant_bound_is_the_one_active_when_the_query_runs(db):
    with Tenancy.scope(1):
        mine = await testmodels.TenantScopedFactory.objects.create(name="mine", company_id=1)
        assert (await testmodels.TenantScopedFactory.objects.get(pk=mine.pk)).name == "mine"
        query = testmodels.TenantScopedFactory.objects.get(pk=mine.pk)
        assert get_direct_plan(query) is not None
    with Tenancy.scope(2):
        with pytest.raises(DoesNotExist):
            await query
        with pytest.raises(DoesNotExist):
            await testmodels.TenantScopedFactory.objects.get(pk=mine.pk)
    with Tenancy.scope(1):
        later = testmodels.TenantScopedFactory.objects.get(pk=mine.pk)
    with Tenancy.scope(Tenancy.ALL):
        assert (await later).name == "mine"
    with Tenancy.scope(Tenancy.any_of(1, 2)):
        for _ in range(2):
            assert (await testmodels.TenantScopedFactory.objects.get(pk=mine.pk)).name == "mine"
    with Tenancy.scope(Tenancy.any_of(2, 3)):
        assert await testmodels.TenantScopedFactory.objects.get(does_not_exist_exception=None, pk=mine.pk) is None


@requires_features(supports_positional_rows=True)
@pytest.mark.asyncio
async def test_no_active_tenant_raises_when_the_query_runs(db):
    query = testmodels.TenantScopedFactory.objects.get(pk=1)
    assert get_direct_plan(query) is None
    with pytest.raises(QueryError, match="no tenant is active"):
        await query
    with Tenancy.scope(1):
        await testmodels.TenantScopedFactory.objects.create(name="mine", company_id=1)
        later = testmodels.TenantScopedFactory.objects.get(name="mine")
    with pytest.raises(QueryError, match="no tenant is active"):
        await later
