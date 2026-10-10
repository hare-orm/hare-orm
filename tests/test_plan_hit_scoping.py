"""A query running on the statement plan of its shape still honors everything its plan key doesn't
hold: the active tenant, all_tenants()/include_deleted(), and the values bound into a write. Each
test first runs a query once so the next one of the same shape hits the plan."""

import pytest

from hare.exceptions import (
    QueryError,
)
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import Q
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.testmodels import TenantActiveAuthor, TenantScopedFactory, TenantScopedOrder, TenantScopedWidget


async def create_widgets() -> None:
    for company_id in (1, 2):
        factory = await TenantScopedFactory.objects.create(name=f"factory {company_id}", company_id=company_id)
        for index in range(2):
            await TenantScopedWidget.objects.create(
                name=f"widget {company_id}.{index}", company_id=company_id, factory=factory
            )


@pytest.mark.asyncio
async def test_tenant_scoped_read_on_a_plan_reads_the_active_tenant(db):
    await create_widgets()
    hits = StatementPlans.hits
    for company_id in (1, 2, 1):
        with Tenancy.scope(company_id):
            names = (
                await TenantScopedWidget.objects.filter(name__startswith="widget")
                .order_by("name")
                .values_list("name", flat=True)
            )
        assert names == [f"widget {company_id}.0", f"widget {company_id}.1"]
    assert StatementPlans.hits - hits >= 2


@pytest.mark.asyncio
async def test_tenant_scoped_read_on_a_plan_without_a_tenant_raises(db):
    await create_widgets()
    with Tenancy.scope(1):
        await TenantScopedWidget.objects.filter(name="widget 1.0")
        await TenantScopedWidget.objects.filter(name="widget 1.1")
    with pytest.raises(QueryError):
        await TenantScopedWidget.objects.filter(name="widget 2.0")
    with pytest.raises(QueryError):
        await TenantScopedWidget.objects.get(name="widget 2.0")


@pytest.mark.asyncio
async def test_all_tenants_and_scoped_reads_keep_their_own_plans(db):
    await create_widgets()
    with Tenancy.scope(1):
        for _ in range(2):
            assert await TenantScopedWidget.objects.filter(name__startswith="widget").count() == 2
            assert await TenantScopedWidget.objects.all_tenants().filter(name__startswith="widget").count() == 4
            assert await TenantScopedWidget.objects.filter(name__startswith="widget").count() == 2


@pytest.mark.asyncio
async def test_get_on_a_plan_reads_only_the_active_tenant(db):
    await create_widgets()
    with Tenancy.scope(1):
        await TenantScopedWidget.objects.get(name="widget 1.0")
        await TenantScopedWidget.objects.get(name="widget 1.1")
    with Tenancy.scope(2):
        assert await TenantScopedWidget.objects.get(does_not_exist_exception=None, name="widget 1.0") is None
        assert (await TenantScopedWidget.objects.get(name="widget 2.0")).company_id == 2


@pytest.mark.asyncio
async def test_select_related_on_a_plan_scopes_the_join_by_the_active_tenant(db):
    await create_widgets()
    factory = await TenantScopedFactory.objects.all_tenants().get(name="factory 2")
    widget = await TenantScopedWidget.objects.all_tenants().get(name="widget 2.0")
    await TenantScopedOrder.objects.create(name="order", widget=widget)
    with Tenancy.scope(2):
        for _ in range(2):
            orders = await TenantScopedOrder.objects.all().select_related("widget__factory")
            assert [order.widget.factory.id for order in orders] == [factory.id]
    with Tenancy.scope(1):
        orders = await TenantScopedOrder.objects.all().select_related("widget__factory")
        assert all(order.widget is None for order in orders)


@pytest.mark.asyncio
async def test_update_and_delete_on_a_plan_touch_only_the_active_tenant(db):
    await create_widgets()
    with Tenancy.scope(1):
        await TenantScopedWidget.objects.filter(name="widget 1.0").update(name="renamed 1.0")
        await TenantScopedWidget.objects.filter(name="widget 1.1").update(name="renamed 1.1")
    with Tenancy.scope(2):
        assert await TenantScopedWidget.objects.filter(name="widget 1.0").update(name="stolen") == 0
        await TenantScopedWidget.objects.filter(name="widget 2.0").delete()
    with Tenancy.scope(1):
        assert await TenantScopedWidget.objects.filter(name="widget 2.1").delete() == 0
    names = sorted(await TenantScopedWidget.objects.all_tenants().values_list("name", flat=True))
    assert names == ["renamed 1.0", "renamed 1.1", "widget 2.1"]


@pytest.mark.asyncio
async def test_soft_deleted_rows_on_a_plan_stay_hidden_unless_included(db):
    for company_id in (1, 1):
        await TenantActiveAuthor.objects.create(name="kept", company_id=company_id)
    deleted = await TenantActiveAuthor.objects.create(name="gone", company_id=1)
    with Tenancy.scope(1):
        await TenantActiveAuthor.objects.filter(id=deleted.id).delete()
        for _ in range(2):
            assert await TenantActiveAuthor.objects.filter(name__in=["kept", "gone"]).count() == 2
            assert await TenantActiveAuthor.objects.include_deleted().filter(name__in=["kept", "gone"]).count() == 3


@pytest.mark.asyncio
async def test_negated_and_plain_conditions_keep_their_own_plans(db):
    await create_widgets()
    with Tenancy.scope(1):
        for _ in range(2):
            assert await TenantScopedWidget.objects.filter(Q(name="widget 1.0")).count() == 1
            assert await TenantScopedWidget.objects.filter(~Q(name="widget 1.0")).count() == 1
            assert await TenantScopedWidget.objects.exclude(name="widget 1.0").count() == 1
            assert await TenantScopedWidget.objects.filter(Q(name="widget 1.0") | Q(name="widget 1.1")).count() == 2
            assert await TenantScopedWidget.objects.filter(Q(name="widget 1.0"), Q(name="widget 1.1")).count() == 0


@pytest.mark.asyncio
async def test_none_and_value_conditions_keep_their_own_plans(db):
    await create_widgets()
    with Tenancy.scope(1):
        for _ in range(2):
            assert await TenantScopedWidget.objects.filter(factory_id=None).count() == 0
            factory = await TenantScopedFactory.objects.get(name="factory 1")
            assert await TenantScopedWidget.objects.filter(factory_id=factory.id).count() == 2
            assert await TenantScopedWidget.objects.filter(factory_id=None).count() == 0
            assert await TenantScopedWidget.objects.filter(id__in=[]).count() == 0
            widget_ids = await TenantScopedWidget.objects.all().values_list("id", flat=True)
            assert await TenantScopedWidget.objects.filter(id__in=widget_ids).count() == 2
            assert await TenantScopedWidget.objects.filter(id__in=widget_ids[:1]).count() == 1
