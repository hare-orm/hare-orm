"""count() and exists() past a keyset boundary or an OFFSET, and a filter on a set operation's
rows (``pk__in=a.union(b)``), keep a statement plan: the boundary values, the OFFSET and the
union's own values are bound per query. Each test compares a plan hit with the same query built in
full."""

import pytest

from tests.shared_models.testmodels import JSONFields, Tournament
from tests.utils.statement_plan_checks import StatementPlanChecks


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
    await StatementPlanChecks.assert_a_later_query_runs_on_its_plan(build, values)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_method", ("after_cursor", "before_cursor"))
@pytest.mark.parametrize("result_type", ("models", "values", "count", "exists"))
async def test_json_cursor_boundaries_rebind_on_a_kept_plan(db, cursor_method, result_type):
    for row_id, value in enumerate((9, 9, 10, 11), 1):
        await JSONFields.objects.create(id=row_id, data=value)

    def build(boundary):
        queryset = getattr(JSONFields.objects.order_by("data", "id"), cursor_method)(*boundary)
        if result_type == "values":
            return queryset.values_list("id", flat=True)
        if result_type == "models":
            return queryset
        return getattr(queryset, result_type)()

    await StatementPlanChecks.assert_a_later_query_runs_on_its_plan(build, ((9, 1), (11, 4)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "build",
    [
        lambda offset: Tournament.objects.order_by("id")[offset : offset + 3].exists(),
        lambda offset: Tournament.objects.offset(offset).exists(),
        lambda offset: Tournament.objects.filter(name__gte="a").offset(offset).limit(3).exists(),
    ],
    ids=["slice", "offset", "offset_and_limit"],
)
async def test_exists_past_an_offset_after_the_same_query_without_one(db, build):
    await create_tournaments()
    # Without an offset the query has none in its SQL - a later one with an offset isn't its plan.
    assert await build(0) is True
    assert await build(100) is False
    assert await build(0) is True
    assert await build(4) is True
