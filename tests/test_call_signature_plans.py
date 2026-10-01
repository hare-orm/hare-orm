"""A queryset made by simple calls alone runs on the plan kept under the key of its calls - its
filters aren't even built - and gives the rows of its own values; any other call leaves it to the
usual build."""

from unittest.mock import patch

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import QueryError
from hare.models.tenancy import Tenancy
from hare.query.expressions import F, Q
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.query_spec import QuerySpec
from hare.query.statements.awaitable_query import AwaitableQuery
from tests.testmodels import IntFields, TenantScopedFactory, Tournament


def spy_on_builds():
    """Counts the queries built - a query running on the plan of its calls builds none."""
    return patch.object(AwaitableQuery, "_make_query", autospec=True, side_effect=AwaitableQuery._make_query)


def spy_on_filter_builds():
    """Counts the kept filter calls built into conditions."""
    return patch.object(
        QuerySpec,
        "_build_pending_filter_calls",
        autospec=True,
        side_effect=QuerySpec._build_pending_filter_calls,
    )


@pytest.mark.asyncio
async def test_a_later_chain_of_the_same_calls_builds_nothing(db):
    for number in range(1, 6):
        await IntFields.objects.create(intnum=number, intnum_null=number * 10)
    assert (await IntFields.objects.filter(intnum__gte=2).order_by("intnum").first()).intnum == 2
    assert await IntFields.objects.filter(intnum__gte=3).count() == 3
    with spy_on_builds() as builds, spy_on_filter_builds() as filter_builds:
        assert (await IntFields.objects.filter(intnum__gte=4).order_by("intnum").first()).intnum == 4
        assert await IntFields.objects.filter(intnum__gte=5).count() == 1
        assert await IntFields.objects.filter(intnum__gte=6).order_by("intnum").first() is None
    assert builds.call_count == 0
    assert filter_builds.call_count == 0


@pytest.mark.asyncio
async def test_values_and_updates_run_on_the_plan_of_their_calls(db):
    for number in range(1, 4):
        await IntFields.objects.create(intnum=number)
    assert list(await IntFields.objects.filter(intnum__gte=2).values_list("intnum", flat=True)) == [2, 3]
    assert await IntFields.objects.filter(intnum=1).update(intnum_null=7) == 1
    hits = StatementPlans.hits
    with spy_on_builds() as builds:
        assert list(await IntFields.objects.filter(intnum__gte=3).values_list("intnum", flat=True)) == [3]
        assert await IntFields.objects.filter(intnum=2).update(intnum_null=8) == 1
    assert builds.call_count == 0
    assert StatementPlans.hits - hits == 2
    assert {row.intnum: row.intnum_null for row in await IntFields.objects.all()} == {1: 7, 2: 8, 3: None}


@pytest.mark.asyncio
async def test_other_calls_take_the_usual_build_and_stay_correct(db):
    for number in range(1, 6):
        await IntFields.objects.create(intnum=number)
    plain = IntFields.objects.filter(intnum__gte=2)
    assert await plain.count() == 4
    assert await plain.filter(Q(intnum=2) | Q(intnum=5)).count() == 2
    assert await plain.exclude(intnum=3).count() == 3
    assert await plain.filter(intnum__lt=F("intnum") + 1).count() == 4
    assert [row.intnum for row in await plain.annotate(double=F("intnum") * 2).filter(double__gt=6)] == [4, 5]
    # The kept filter calls are built when a later call needs the conditions.
    assert await plain.distinct().count() == 4
    assert await plain.filter(intnum__lte=3).filter(intnum__gte=3).count() == 1


@pytest.mark.asyncio
async def test_the_tenant_bound_is_the_one_active_when_the_query_runs(db):
    with Tenancy.scope(1):
        await TenantScopedFactory.objects.create(name="one", company_id=1)
    with Tenancy.scope(2):
        await TenantScopedFactory.objects.create(name="two", company_id=2)
    for tenant, expected in ((1, ["one"]), (2, ["two"]), (1, ["one"])):
        with Tenancy.scope(tenant):
            assert [row.name for row in await TenantScopedFactory.objects.filter(name__in=["one", "two"])] == expected
    with Tenancy.scope(Tenancy.any_of(1, 2)):
        names = await TenantScopedFactory.objects.filter(name__in=["one", "two"]).order_by("name")
        assert [row.name for row in names] == ["one", "two"]


@pytest.mark.asyncio
async def test_an_instance_filter_value_runs_on_its_plan(db):
    first = await Tournament.objects.create(name="first")
    second = await Tournament.objects.create(name="second")
    assert (await Tournament.objects.filter(pk=first.pk).first()).name == "first"
    with spy_on_builds() as builds:
        assert (await Tournament.objects.filter(pk=second.pk).first()).name == "second"
    assert builds.call_count == 0


@requires_features(supports_select_for_update=True, supports_transactions=True)
@pytest.mark.asyncio
async def test_a_lock_outside_a_transaction_is_refused_on_a_direct_get(db_truncate):
    # db_truncate: the db fixture's own transaction would hold every query here.
    tournament = await Tournament.objects.create(name="locked")
    from hare.transactions.transactions import Transactions

    async with Transactions.atomic():
        assert (await Tournament.objects.select_for_update().get(pk=tournament.pk)).name == "locked"
        assert (await Tournament.objects.select_for_update().get(pk=tournament.pk)).name == "locked"
    with pytest.raises(QueryError, match="requires an active Transactions.atomic"):
        await Tournament.objects.select_for_update().get(pk=tournament.pk)
