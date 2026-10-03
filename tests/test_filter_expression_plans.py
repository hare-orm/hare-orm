"""A filter on an ``Exists(...)`` condition and a filter whose value is an expression -
``F(...)``, arithmetic, ``OuterRef(...)`` in a correlated subquery - keep a plan: the expression
records its own values while it is resolved. Each test compares a plan hit with the same query
built in full."""

from unittest.mock import patch

import pytest

from hare.models.tenancy import Tenancy
from hare.query.expressions import Exists, F, OuterRef, Q, Subquery
from hare.query.plans.statement_plans import StatementPlans
from hare.query.statements.awaitable_query import AwaitableQuery
from tests.testmodels import Event, IntFields, TenantScopedOrder, TenantScopedWidget, Tournament


def full_build():
    """Runs a query built in full, as it ran before plans."""
    return patch.object(AwaitableQuery, "_run_on_plan", lambda self, *args, **kwargs: False)


async def create_tournaments() -> None:
    for index, name in enumerate(("a", "b", "c", "d"), start=1):
        tournament = await Tournament.objects.create(name=name)
        for event_index in range(index):
            await Event.objects.create(name=f"{name}{event_index}", tournament=tournament)
    for intnum in (1, 2, 3, 4):
        await IntFields.objects.create(intnum=intnum, intnum_null=intnum * 2 if intnum % 2 else None)


def names(queryset):
    return queryset.order_by("name").values_list("name", flat=True)


SHAPES = [
    (
        "exists condition",
        lambda value: names(Tournament.objects.filter(Exists(Event.objects.filter(name=value)))),
        ("a0", "zz"),
    ),
    (
        "negated exists condition",
        lambda value: names(Tournament.objects.filter(~Exists(Event.objects.filter(name=value)))),
        ("a0", "zz"),
    ),
    (
        "correlated exists condition",
        lambda value: names(
            Tournament.objects.filter(Exists(Event.objects.filter(tournament_id=OuterRef("id"), name__gte=value)))
        ),
        ("b", "c2"),
    ),
    (
        "correlated exists annotation",
        lambda value: names(
            Tournament.objects.annotate(
                has_events=Exists(Event.objects.filter(tournament_id=OuterRef("id"), name__gte=value))
            ).filter(has_events=True)
        ),
        ("b", "c2"),
    ),
    (
        "correlated subquery value",
        lambda value: names(
            Tournament.objects.filter(
                id__in=Subquery(
                    Event.objects.filter(tournament_id=OuterRef("id"), name__gte=value).values_list(
                        "tournament_id", flat=True
                    )
                )
            )
        ),
        ("b", "c2"),
    ),
    (
        "field reference",
        lambda value: (
            IntFields.objects.filter(intnum_null=F("intnum") * value)
            .order_by("intnum")
            .values_list("intnum", flat=True)
        ),
        (2, 3),
    ),
    (
        "field reference in an OR",
        lambda value: (
            IntFields.objects.filter(Q(intnum_null__gt=F("intnum") + value) | Q(intnum=4))
            .order_by("intnum")
            .values_list("intnum", flat=True)
        ),
        (0, 1),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "build", "values"), SHAPES, ids=[shape[0] for shape in SHAPES])
async def test_a_later_query_of_the_structure_runs_on_its_plan(db, name, build, values):
    await create_tournaments()
    first_value, second_value = values
    first = await build(first_value)
    with full_build():
        expected = await build(second_value)
    assert expected != first
    hits = StatementPlans.hits
    assert await build(second_value) == expected
    assert StatementPlans.hits - hits == 1


@pytest.mark.asyncio
async def test_a_reference_to_an_annotation_of_the_enclosing_query_keeps_no_plan(db):
    """Resolving the enclosing query's annotation again inside the subquery records its values out
    of the order the plan would list them in."""
    await create_tournaments()

    def tournaments(minimum_events: int):
        return names(
            Tournament.objects.annotate(threshold=F("id") * 0 + minimum_events).filter(
                Exists(Event.objects.filter(tournament_id=OuterRef("id"), event_id__gte=OuterRef("threshold")))
            )
        )

    first = await tournaments(1)
    hits = StatementPlans.hits
    second = await tournaments(1000)
    assert StatementPlans.hits == hits
    assert first == ["a", "b", "c", "d"]
    assert second == []


@pytest.mark.asyncio
async def test_a_field_reference_across_a_tenant_scoped_relation_keeps_a_plan_binding_the_active_tenant(db):
    """The JOIN of a relation with a default scope holds the active tenant's condition - a plan
    would keep the first tenant's.

    The JOIN's scope is recorded apart from the query's own values - the plan keeps it and binds
    the active tenant's on each run."""
    widget_1 = await TenantScopedWidget.objects.create(name="O1", company_id=1)
    widget_2 = await TenantScopedWidget.objects.create(name="O2", company_id=2)
    await TenantScopedOrder.objects.create(name="O1", widget=widget_1)
    await TenantScopedOrder.objects.create(name="O2", widget=widget_2)

    def matching():
        return TenantScopedOrder.objects.filter(name=F("widget__name")).order_by("name").values_list("name", flat=True)

    StatementPlans.plans.clear()
    cache_size_before = 0
    with Tenancy.scope(1):
        first = await matching()
    with Tenancy.scope(2):
        second = await matching()
    assert len(StatementPlans.plans) > cache_size_before
    assert first == ["O1"]
    assert second == ["O2"]
