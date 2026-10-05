"""A queryset made by simple calls alone runs on the plan kept under the key of its calls - its
filters aren't even built - and gives the rows of its own values; any other call leaves it to the
usual build."""

from unittest.mock import patch

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import DoesNotExist, QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.enums import Connector
from hare.query.expressions import F, Q
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.pending_calls.pending_filter_calls import PendingFilterCalls
from hare.query.statements.awaitable_query import AwaitableQuery
from tests.testmodels import IntFields, SoftDeleteStandalone, TenantScopedFactory, Tournament

# Counts the builds a run on a plan saves - plan verification compares after the body.
pytestmark = pytest.mark.plan_verification_after_test


def spy_on_builds():
    """Counts the queries built - a query running on the plan of its calls builds none."""
    return patch.object(AwaitableQuery, "_make_query", autospec=True, side_effect=AwaitableQuery._make_query)


def spy_on_filter_builds():
    """Counts the kept filter calls built into conditions."""
    return patch.object(
        PendingFilterCalls,
        "build_pending_filter_calls",
        autospec=True,
        side_effect=PendingFilterCalls.build_pending_filter_calls,
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
    assert sorted(row.intnum for row in await plain.annotate(double=F("intnum") * 2).filter(double__gt=6)) == [4, 5]
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


@requires_features(supports_select_for_update=True, supports_transactions=True)
@pytest.mark.asyncio
async def test_a_get_with_a_row_lock_runs_on_the_plan_of_its_call(db_truncate):
    from hare.transactions.transactions import Transactions

    tournament = await Tournament.objects.create(name="locked")
    async with Transactions.atomic():
        assert (await Tournament.objects.select_for_update().get(pk=tournament.pk)).name == "locked"
        with spy_on_builds() as builds:
            assert (await Tournament.objects.select_for_update().get(pk=tournament.pk)).name == "locked"
            assert (await Tournament.objects.get(pk=tournament.pk)).name == "locked"
    # The plain get() of the same key took a plan of its own - one build, not the locking one.
    assert builds.call_count == 1


@pytest.mark.asyncio
async def test_a_get_seeing_deleted_rows_runs_on_a_plan_of_its_own(db):
    kept = await SoftDeleteStandalone.objects.create(name="kept")
    removed = await SoftDeleteStandalone.objects.create(name="removed")
    await removed.delete()
    for _ in range(2):
        assert (await SoftDeleteStandalone.objects.include_deleted().get(pk=removed.pk)).name == "removed"
        assert (await SoftDeleteStandalone.objects.only_deleted().get(pk=removed.pk)).name == "removed"
        with pytest.raises(DoesNotExist):
            await SoftDeleteStandalone.objects.get(pk=removed.pk)
        with pytest.raises(DoesNotExist):
            await SoftDeleteStandalone.objects.only_deleted().get(pk=kept.pk)
    with spy_on_builds() as builds:
        assert (await SoftDeleteStandalone.objects.include_deleted().get(pk=kept.pk)).name == "kept"
        assert (await SoftDeleteStandalone.objects.get(pk=kept.pk)).name == "kept"
    assert builds.call_count == 0


@pytest.mark.asyncio
async def test_a_plan_found_by_a_query_of_other_calls_is_kept_under_these_calls_too(db):
    """A query whose statement plan another queryset recorded runs on that plan when built - and
    keeps it under its own calls, so the next one builds nothing."""
    row = await IntFields.objects.create(intnum=4)
    StatementPlans.forget_all()
    assert (await IntFields.objects.get(Q(pk=row.pk))).intnum == 4
    StatementPlans.call_signature_plans.clear()
    assert (await IntFields.objects.get(pk=row.pk)).intnum == 4
    with spy_on_builds() as builds:
        assert (await IntFields.objects.get(pk=row.pk)).intnum == 4
    assert builds.call_count == 0


@pytest.mark.asyncio
async def test_get_values_a_plan_binds_otherwise_take_the_filter_calls(db):
    """A None, a list or an __isnull boolean doesn't bind as given - such a get() is a filter() call
    of the signature, and still runs on its plan."""
    row = await IntFields.objects.create(intnum=6, intnum_null=None)
    for kwargs in ({"intnum_null": None, "intnum": 6}, {"intnum__in": [6, 7]}, {"intnum_null__isnull": True}):
        assert (await IntFields.objects.get(**kwargs)).pk == row.pk
        with spy_on_builds() as builds:
            assert (await IntFields.objects.get(**kwargs)).pk == row.pk
        assert builds.call_count == 0, kwargs


@pytest.mark.asyncio
async def test_q_conditions_of_plain_filters_run_on_the_plan_of_their_calls(db):
    """Q conditions of plain filters are part of the calls - their tree in the key, their values
    bound - so the same trees with other values build nothing and give their own rows."""
    for number in range(1, 7):
        await IntFields.objects.create(intnum=number, intnum_null=number * 10 if number % 2 else None)

    def get_querysets(first: int, second: int, numbers: list[int]):
        return [
            IntFields.objects.filter(Q(intnum=first) | Q(intnum=second)),
            IntFields.objects.filter(Q(intnum=first) | Q(intnum=second), intnum_null__isnull=False),
            IntFields.objects.exclude(~Q(intnum__in=numbers), Q(intnum__gte=first)),
            IntFields.objects.filter(Q(Q(intnum=first) | Q(intnum=second)) & ~Q(intnum_null=None)),
            IntFields.objects.filter(Q.with_connector(Connector.OR, intnum=first, intnum_null=second * 10)),
        ]

    async def read(querysets):
        return [sorted(await queryset.values_list("intnum", flat=True)) for queryset in querysets] + [
            await querysets[0].count()
        ]

    assert await read(get_querysets(1, 2, [1, 2])) == [[1, 2], [1], [1, 2], [1], [1], 2]
    with spy_on_builds() as builds, spy_on_filter_builds() as filter_builds:
        assert await read(get_querysets(3, 6, [5, 6])) == [[3, 6], [3], [1, 2, 5, 6], [3], [3], 2]
    assert builds.call_count == 0
    assert filter_builds.call_count == 0


@pytest.mark.asyncio
async def test_q_conditions_of_another_tree_take_a_plan_of_their_own(db):
    """An AND tree and an OR tree of the same filters, or lists of other lengths, are other plans."""
    for number in range(1, 5):
        await IntFields.objects.create(intnum=number, intnum_null=number)
    assert await IntFields.objects.filter(Q(intnum=1) | Q(intnum_null=2)).count() == 2
    assert await IntFields.objects.filter(Q(intnum=1) & Q(intnum_null=2)).count() == 0
    assert await IntFields.objects.filter(Q(intnum=2) & Q(intnum_null=2)).count() == 1
    assert await IntFields.objects.filter(Q(intnum__in=[1, 2]) | Q(intnum=4)).count() == 3
    assert await IntFields.objects.filter(Q(intnum__in=[1]) | Q(intnum=4)).count() == 2
    assert await IntFields.objects.filter(~Q(intnum=1)).count() == 3
    assert await IntFields.objects.filter(Q(intnum=1)).count() == 1


@pytest.mark.asyncio
async def test_q_conditions_of_expressions_run_on_the_plan_of_their_calls(db):
    """A Q condition holding an expression or a query is kept unbuilt too - described by its own
    description."""
    for number in range(1, 4):
        await IntFields.objects.create(intnum=number, intnum_null=number)
    assert await IntFields.objects.filter(Q(intnum=F("intnum_null")) | Q(intnum=9)).count() == 3
    assert await IntFields.objects.filter(Q(intnum=F("intnum_null")) | Q(intnum=9)).count() == 3
    assert (
        await IntFields.objects.filter(Q(intnum__in=IntFields.objects.filter(intnum=2).values("intnum"))).count() == 1
    )
    with spy_on_filter_builds() as filter_builds:
        assert await IntFields.objects.filter(Q(intnum=F("intnum_null")) | Q(intnum=9)).count() == 3
    assert filter_builds.call_count == 0


@pytest.mark.asyncio
async def test_annotations_run_on_the_plan_of_their_calls(db):
    """annotate()/alias() expressions are part of the calls - described by their own descriptions,
    their literals bound - so a later chain of the same calls builds nothing."""
    for number in range(1, 6):
        await IntFields.objects.create(intnum=number, intnum_null=number * 10)

    def read(minimum: int, factor: int):
        return (
            IntFields.objects.filter(intnum__gte=minimum)
            .annotate(scaled=F("intnum") * factor)
            .alias(shifted=F("intnum_null") + factor)
            .filter(shifted__gt=0)
            .order_by("intnum")
            .values_list("intnum", "scaled")
        )

    assert list(await read(4, 2)) == [(4, 8), (5, 10)]
    with spy_on_builds() as builds, spy_on_filter_builds() as filter_builds:
        # The annotation's value is bound before the filter's, in the order of the calls the other
        # way round.
        assert list(await read(3, 3)) == [(3, 9), (4, 12), (5, 15)]
    assert builds.call_count == 0
    assert filter_builds.call_count == 0


@pytest.mark.asyncio
async def test_exists_and_subquery_filters_run_on_the_plan_of_their_calls(db):
    from hare.query.expressions import Exists, OuterReference, Subquery

    for number in range(1, 6):
        await IntFields.objects.create(intnum=number, intnum_null=number if number % 2 else None)

    def read(minimum: int, maximum: int):
        higher = IntFields.objects.filter(intnum_null=OuterReference("intnum"), intnum__gte=minimum)
        lower = IntFields.objects.filter(intnum__lte=maximum).values("id")
        return (
            IntFields.objects.filter(Exists(higher), id__in=Subquery(lower))
            .distinct()
            .order_by("intnum")
            .values_list("intnum", flat=True)
        )

    assert list(await read(1, 5)) == [1, 3, 5]
    with spy_on_builds() as builds:
        assert list(await read(2, 4)) == [3]
        assert list(await read(1, 1)) == [1]
    assert builds.call_count == 0
