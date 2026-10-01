"""A query with CTEs (``.with_cte(name, query)``) keeps its plan with them: each body's shape is part
of the key and its values are bound per query. A queryset built into another query - a CTE body,
``Subquery(queryset)`` - keeps its part of the enclosing plan the way ``.values()`` does. Each test
runs its queries at least twice, so both the build and the plan hit return the same rows."""

import pytest

from hare.contrib.test import requires_features
from hare.query.expressions import RawSQL, Subquery
from hare.query.functions import Count
from hare.query.plans.statement_plans import StatementPlans
from tests.testmodels import Event, Tournament


async def count_plan_hits(statement) -> tuple[object, int]:
    hits = StatementPlans.hits
    result = await statement
    return result, StatementPlans.hits - hits


async def create_tournaments() -> list[Tournament]:
    tournaments = [await Tournament.objects.create(name=name) for name in ("a", "b", "c")]
    for tournament in tournaments:
        await Event.objects.create(name=f"{tournament.name}-event", tournament=tournament)
    return tournaments


def cte_filtered_events(tournament_name: str):
    """Events of the tournaments a CTE picks by name."""
    return (
        Event.objects.filter(tournament_id__in=RawSQL('SELECT "id" FROM "picked"'))
        .with_cte("picked", Tournament.objects.filter(name=tournament_name).values("id"))
        .order_by("name")
    )


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_cte_values_are_bound_per_query(db):
    await create_tournaments()
    assert [event.name for event in await cte_filtered_events("a")] == ["a-event"]
    rows, hits = await count_plan_hits(cte_filtered_events("b"))
    assert [event.name for event in rows] == ["b-event"]
    assert hits == 1
    assert await cte_filtered_events("nobody") == []


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_aggregate_with_a_cte_runs_on_its_plan(db):
    await create_tournaments()

    def event_count(tournament_name: str):
        return (
            Event.objects.filter(tournament_id__in=RawSQL('SELECT "id" FROM "picked"'))
            .with_cte("picked", Tournament.objects.filter(name__gte=tournament_name).values("id"))
            .aggregate(total=Count("event_id"))
        )

    assert await event_count("a") == {"total": 3}
    for tournament_name, expected in (("b", 2), ("c", 1), ("d", 0)):
        result, hits = await count_plan_hits(event_count(tournament_name))
        assert (result, hits) == ({"total": expected}, 1)


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_cte_with_a_queryset_body(db):
    await create_tournaments()

    def query(name: str):
        return (
            Event.objects.filter(tournament_id__in=RawSQL('SELECT "id" FROM "picked"'))
            .with_cte("picked", Tournament.objects.filter(name=name))
            .order_by("name")
        )

    for name in ("a", "b", "c", "a"):
        assert [event.name for event in await query(name)] == [f"{name}-event"]


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_count_exists_and_values_with_a_cte(db):
    await create_tournaments()
    for name, expected in (("a", 1), ("nobody", 0), ("b", 1)):
        query = cte_filtered_events(name)
        assert await query.count() == expected
        assert await query.exists() is bool(expected)
        assert await query.values_list("name", flat=True) == ([f"{name}-event"] if expected else [])


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_cte_body_slice_is_bound_per_query(db):
    tournaments = await create_tournaments()

    def query(start: int):
        return Tournament.objects.filter(id__in=RawSQL('SELECT "id" FROM "picked"')).with_cte(
            "picked", Tournament.objects.all().order_by("id").values("id")[start : start + 1]
        )

    for start in (0, 1, 2, 0):
        assert [tournament.id for tournament in await query(start)] == [tournaments[start].id]


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_subquery_of_a_queryset(db):
    await create_tournaments()
    for name in ("a", "b"):
        rows = await Event.objects.filter(
            tournament__in=Subquery(Tournament.objects.filter(name=name).only("id"))
        ).order_by("name")
        assert [event.name for event in rows] == [f"{name}-event"]


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_sql_of_a_query_with_a_cte_holds_its_own_values(db):
    await create_tournaments()
    for name in ("a", "b"):
        await cte_filtered_events(name)
    assert "zzz" in cte_filtered_events("zzz").sql(params_inline=True)


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_raw_sql_filter_value_binds_its_parameters(db):
    tournaments = await create_tournaments()

    def query(name: str):
        return Tournament.objects.filter(id__in=RawSQL('SELECT "id" FROM "tournament" WHERE "name" = %s', [name]))

    await query("a")
    for tournament in (*tournaments, tournaments[0]):
        rows, hits = await count_plan_hits(query(tournament.name))
        assert [row.id for row in rows] == [tournament.id]
        assert hits == 1
    rows = await Tournament.objects.filter(id__in=RawSQL('SELECT "id" FROM "tournament" WHERE "name" <> %s', ["a"]))
    assert sorted(row.name for row in rows) == ["b", "c"]
