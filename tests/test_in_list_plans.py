"""An ``__in``/``__not_in`` list keeps a statement plan whatever it holds: the number of values other
than None and whether it holds a None are part of the plan key, those other values are bound, and an
empty list - or one of none but None - is a constant condition binding nothing. Each test compares a
plan hit with the same query built in full."""

from unittest.mock import patch

import pytest

from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.testmodels import IntFields


def full_build():
    """Runs a query built in full, as it ran before plans."""
    return patch.object(StatementPlanRuns, "run_on_plan", lambda *args, **kwargs: False)


async def create_numbers() -> None:
    for intnum in (1, 2, 3, 4):
        await IntFields.objects.create(intnum=intnum, intnum_null=intnum if intnum % 2 else None)


def matching(**filters):
    return IntFields.objects.filter(**filters).order_by("intnum").values_list("intnum", flat=True)


SHAPES = [
    ("in with a None", lambda values: matching(intnum_null__in=values), ([1, None], [3, None])),
    ("not in with a None", lambda values: matching(intnum_null__not_in=values), ([1, None], [3, None])),
    (
        "in of none but None",
        lambda values: matching(intnum__lte=values[0], intnum_null__in=values[1]),
        ((2, [None]), (4, [None])),
    ),
    ("empty in", lambda values: matching(intnum__gte=values[0], intnum__in=values[1]), ((1, []), (2, []))),
    ("empty not in", lambda values: matching(intnum__gte=values[0], intnum__not_in=values[1]), ((1, []), (3, []))),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "build", "values"), SHAPES, ids=[shape[0] for shape in SHAPES])
async def test_a_later_query_of_the_structure_runs_on_its_plan(db, name, build, values):
    await create_numbers()
    first_value, second_value = values
    first = await build(first_value)
    with full_build():
        expected = await build(second_value)
    assert expected != first or name == "empty in"
    hits = StatementPlans.hits
    assert await build(second_value) == expected
    assert StatementPlans.hits - hits == 1


@pytest.mark.asyncio
async def test_a_list_with_a_none_never_shares_a_plan_with_one_without(db):
    await create_numbers()
    assert await matching(intnum_null__in=[1, 3]) == [1, 3]
    assert await matching(intnum_null__in=[1, None]) == [1, 2, 4]
    assert await matching(intnum_null__in=[3, 5]) == [3]
    assert await matching(intnum_null__in=[3, None]) == [2, 3, 4]
