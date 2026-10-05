"""aggregate() keeps a plan whatever builds it - prior annotations the metrics or filters read, a
grouped derived table for an aggregate annotation or .distinct(), a .group_by(): the steps of the
build that recorded values are kept with the plan, and a later query binds its own values in that
order. Each test compares a plan hit with the same query built in full."""

from unittest.mock import patch

import pytest

from hare.query.expressions import Case, F, When
from hare.query.functions import Count, Max, Sum
from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.testmodels import Event, Tournament


def full_build():
    """Runs a query built in full, as it ran before plans."""
    return patch.object(StatementPlanRuns, "run_on_plan", lambda *args, **kwargs: False)


async def create_tournaments() -> list[Tournament]:
    tournaments = []
    for index, name in enumerate(("a", "b", "c", "d"), start=1):
        tournament = await Tournament.objects.create(name=name)
        tournaments.append(tournament)
        for event_index in range(index):
            await Event.objects.create(name=f"{name}-event-{event_index}", tournament=tournament)
    return tournaments


SHAPES = [
    (
        "metric over an annotation holding a literal",
        lambda value: Tournament.objects.annotate(scaled=F("id") * value).aggregate(total=Sum("scaled")),
        (2, 3),
    ),
    (
        "filter on an aggregate annotation",
        lambda value: (
            Tournament.objects.annotate(event_count=Count("events"))
            .filter(event_count__gte=value)
            .aggregate(tournaments=Count("id"))
        ),
        (2, 3),
    ),
    (
        "metric over an aggregate annotation",
        lambda value: (
            Tournament.objects.filter(name__lte=value)
            .annotate(event_count=Count("events"))
            .aggregate(most=Max("event_count"))
        ),
        ("a", "c"),
    ),
    (
        "distinct across a relation",
        lambda value: Tournament.objects.filter(events__name__gte=value).distinct().aggregate(count=Count("id")),
        ("a", "c"),
    ),
    (
        "group_by",
        lambda value: (
            Tournament.objects.filter(name__lte=value)
            .annotate(event_count=Count("events"))
            .group_by("name")
            .aggregate(most=Max("event_count"))
        ),
        ("a", "c"),
    ),
    (
        "alias read by a filter",
        lambda value: (
            Tournament.objects.all()
            .alias(rank=Case(When(name="a", then=value), default=0))
            .filter(rank__gt=1)
            .aggregate(count=Count("id"))
        ),
        (1, 3),
    ),
    (
        "none",
        lambda value: Tournament.objects.filter(name=value).none().aggregate(count=Count("id")),
        ("a", "b"),
    ),
]


@pytest.mark.asyncio
async def test_aggregate_after_and_before_a_keyset_boundary_keep_plans_of_their_own(db):
    """.before_cursor() reverses the ordering the boundary compares in - another shape."""
    await create_tournaments()
    ordered = Tournament.objects.all().order_by("name")
    for _ in range(2):
        assert await ordered.after_cursor("b").aggregate(count=Count("id")) == {"count": 2}
        assert await ordered.before_cursor("c").aggregate(count=Count("id")) == {"count": 2}
        assert await ordered.after_cursor("c").aggregate(count=Count("id")) == {"count": 1}
        assert await ordered.before_cursor("b").aggregate(count=Count("id")) == {"count": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "build", "values"), SHAPES, ids=[shape[0] for shape in SHAPES])
async def test_a_later_aggregate_of_the_shape_runs_on_its_plan(db, name, build, values):
    await create_tournaments()
    first_value, second_value = values
    first = await build(first_value)
    with full_build():
        expected = await build(second_value)
    assert expected != first or name == "none"
    hits = StatementPlans.hits
    assert await build(second_value) == expected
    assert StatementPlans.hits - hits == 1
