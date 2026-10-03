"""count() and exists() past a keyset boundary or an OFFSET, and a filter on a set operation's
rows (``pk__in=a.union(b)``), keep a statement plan: the boundary values, the OFFSET and the
union's own values are bound per query. Each test compares a plan hit with the same query built in
full."""

from unittest.mock import patch

import pytest

from hare.query.plans.statement_plans import StatementPlans
from hare.query.statements.awaitable_query import AwaitableQuery
from tests.testmodels import Tournament


def full_build():
    """Runs a query built in full, as it ran before plans."""
    return patch.object(AwaitableQuery, "_run_on_plan", lambda self, *args, **kwargs: False)


async def create_tournaments() -> None:
    for name in ("a", "b", "c", "d", "e"):
        await Tournament.objects.create(name=name)


SHAPES = [
    (
        "count after a keyset boundary",
        lambda value: Tournament.objects.all().order_by("name").after_cursor(value).count(),
        ("a", "c"),
    ),
    (
        "count before a keyset boundary",
        lambda value: Tournament.objects.all().order_by("name").before_cursor(value).count(),
        ("e", "c"),
    ),
    (
        "exists past an offset",
        lambda value: Tournament.objects.filter(name__gte="b").offset(value).exists(),
        (1, 10),
    ),
    (
        "exists after a keyset boundary",
        lambda value: Tournament.objects.all().order_by("name").after_cursor(value).exists(),
        ("a", "e"),
    ),
    (
        "filter on a union's rows",
        lambda value: (
            Tournament.objects.filter(
                pk__in=Tournament.objects.filter(name=value).union(Tournament.objects.filter(name="e"))
            )
            .order_by("name")
            .values_list("name", flat=True)
        ),
        ("a", "b"),
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
