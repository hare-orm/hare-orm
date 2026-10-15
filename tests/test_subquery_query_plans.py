"""A filter whose value is a query - ``field__in=Other.objects.filter(...).values_list(...)``, a bare queryset,
``Subquery(...)`` - keeps its plan with the subquery in it: the subquery's shape is part of the key
and its values, slice included, are bound per query. Each test runs its queries at least twice,
so both the build and the plan hit return the same rows."""

import pytest

from hare.query.expressions import Q, Subquery
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.relation_loading.select import Select
from tests.testmodels import DoubleFK, Employee, Event, SoftDeleteStandalone, Tournament


async def count_plan_hits(statement) -> tuple[object, int]:
    hits = StatementPlans.hits
    result = await statement
    return result, StatementPlans.hits - hits


async def create_tournaments() -> tuple[list[Tournament], list[Event]]:
    tournaments = [await Tournament.objects.create(name=name) for name in ("a", "b", "c")]
    events = [
        await Event.objects.create(name=f"{tournament.name}-event", tournament=tournament)
        for tournament in tournaments
    ]
    return tournaments, events


def names(rows) -> list[str]:
    return sorted(row.name for row in rows)


@pytest.mark.asyncio
async def test_values_list_subquery_runs_on_the_plan_and_binds_its_values(db):
    await create_tournaments()

    def query(tournament_name):
        return Event.objects.filter(
            tournament_id__in=Tournament.objects.filter(name=tournament_name).values_list("id", flat=True)
        ).order_by("name")

    assert names(await query("a")) == ["a-event"]
    rows, hits = await count_plan_hits(query("b"))
    assert names(rows) == ["b-event"]
    assert hits == 1
    assert await query("nobody") == []


@pytest.mark.asyncio
async def test_bare_queryset_and_explicit_subquery(db):
    await create_tournaments()
    for name in ("a", "b"):
        assert names(
            await Event.objects.filter(tournament__in=Tournament.objects.filter(name=name)).order_by("name")
        ) == [f"{name}-event"]
        assert names(
            await Event.objects.filter(
                tournament_id__in=Subquery(Tournament.objects.filter(name=name).values_list("id", flat=True))
            ).order_by("name")
        ) == [f"{name}-event"]


@pytest.mark.asyncio
async def test_subquery_slice_is_bound_per_query(db):
    tournaments, _ = await create_tournaments()
    ordered_ids = Tournament.objects.all().order_by("id").values_list("id", flat=True)
    for start in (0, 1, 2, 0):
        rows = await Tournament.objects.filter(id__in=ordered_ids[start : start + 1])
        assert [row.id for row in rows] == [tournaments[start].id]
    for limit in (1, 2, 3):
        rows = await Tournament.objects.filter(id__in=ordered_ids.limit(limit)).order_by("id")
        assert [row.id for row in rows] == [tournament.id for tournament in tournaments[:limit]]


@pytest.mark.asyncio
async def test_subquery_values_of_other_shapes_keep_their_own_plans(db):
    await create_tournaments()
    for _ in range(2):
        assert names(
            await Event.objects.filter(
                tournament_id__in=Tournament.objects.filter(name="a").values_list("id", flat=True)
            )
        ) == ["a-event"]
        assert names(
            await Event.objects.filter(
                tournament_id__in=Tournament.objects.filter(name__in=["a", "c"]).values_list("id", flat=True)
            )
        ) == ["a-event", "c-event"]
        assert names(
            await Event.objects.filter(
                tournament_id__in=Tournament.objects.exclude(name="a").values_list("id", flat=True)
            )
        ) == [
            "b-event",
            "c-event",
        ]
        assert names(
            await Event.objects.exclude(
                tournament_id__in=Tournament.objects.filter(name="a").values_list("id", flat=True)
            )
        ) == ["b-event", "c-event"]


@pytest.mark.asyncio
async def test_nested_subqueries(db):
    await create_tournaments()

    def query(event_name):
        return Tournament.objects.filter(
            id__in=Event.objects.filter(
                tournament_id__in=Tournament.objects.filter(
                    id__in=Event.objects.filter(name=event_name).values_list("tournament_id", flat=True)
                ).values_list("id", flat=True)
            ).values_list("tournament_id", flat=True)
        )

    for event_name in ("a-event", "b-event", "c-event"):
        assert names(await query(event_name)) == [event_name[0]]


@pytest.mark.asyncio
async def test_subquery_over_the_same_table(db):
    boss = await Employee.objects.create(name="boss")
    first = await Employee.objects.create(name="first", manager=boss)
    await Employee.objects.create(name="second", manager=first)
    for manager_name, expected in (("boss", ["first"]), ("first", ["second"]), ("second", [])):
        rows = await Employee.objects.filter(
            manager_id__in=Employee.objects.filter(name=manager_name).values_list("id", flat=True)
        ).order_by("name")
        assert [row.name for row in rows] == expected


@pytest.mark.asyncio
async def test_subquery_with_a_default_scope_binds_it_per_query(db):
    kept = await SoftDeleteStandalone.objects.create(name="kept")
    removed = await SoftDeleteStandalone.objects.create(name="removed")
    await removed.delete()
    for _ in range(2):
        rows = await SoftDeleteStandalone.objects.include_deleted().filter(
            id__in=SoftDeleteStandalone.objects.filter(name__in=["kept", "removed"]).values_list("id", flat=True)
        )
        assert [row.id for row in rows] == [kept.id]


@pytest.mark.asyncio
async def test_subquery_with_an_extra_condition_keeps_a_plan_of_its_own(db):
    """A select_related() extra condition is part of a subquery's shape - a subquery with one runs
    on a plan of its own, binding the condition's values, never on the plan of the same shape
    without it."""
    leaf = await DoubleFK.objects.create(name="leaf", left=None)
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)
    plain_inner = DoubleFK.objects.filter(pk=root.pk).values_list("left__left__name", flat=True)
    for _ in range(2):
        assert [row.id for row in await DoubleFK.objects.filter(name__in=plain_inner)] == [leaf.id]
    for extra_condition_name, expected in (("not-leaf", []), ("leaf", [leaf.id]), ("not-leaf", [])):
        inner = (
            DoubleFK.objects.filter(pk=root.pk)
            .select_related("left", Select("left__left", extra_condition=Q(name=extra_condition_name)))
            .values_list("left__left__name", flat=True)
        )
        assert [row.id for row in await DoubleFK.objects.filter(name__in=inner)] == expected


@pytest.mark.asyncio
async def test_query_shown_after_a_plan_hit_holds_its_own_values(db):
    """A query built into SQL text without running (``.sql()``) takes no plan's text - with a
    subquery in the plan it is built in full, never from a copy that would keep the first
    query's subquery values."""
    await create_tournaments()
    for name in ("a", "b"):
        await Event.objects.filter(tournament_id__in=Tournament.objects.filter(name=name).values_list("id", flat=True))
    sql = Event.objects.filter(
        tournament_id__in=Tournament.objects.filter(name="zzz").values_list("id", flat=True)
    ).sql(parameters_inline=True)
    assert "zzz" in sql
