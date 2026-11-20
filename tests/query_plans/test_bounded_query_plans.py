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
        (4, 2),
    ),
    (
        "count before a keyset boundary",
        lambda value: Tournament.objects.all().order_by("name").before_cursor(value).count(),
        ("e", "c"),
        (4, 2),
    ),
    (
        "exists past an offset",
        lambda value: Tournament.objects.filter(name__gte="b").offset(value).exists(),
        (1, 10),
        (True, False),
    ),
    (
        "exists after a keyset boundary",
        lambda value: Tournament.objects.all().order_by("name").after_cursor(value).exists(),
        ("a", "e"),
        (True, False),
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
        (["a", "e"], ["b", "e"]),
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "build", "values", "expected"), SHAPES, ids=[shape[0] for shape in SHAPES])
async def test_a_later_query_of_the_structure_runs_on_its_plan(db, name, build, values, expected):
    await create_tournaments()
    await StatementPlanChecks.assert_a_later_query_runs_on_its_plan(build, values, expected_results=expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor_method", ("after_cursor", "before_cursor"))
@pytest.mark.parametrize("result_type", ("models", "values", "count", "exists"))
async def test_json_cursor_boundaries_rebind_on_a_kept_plan(db, cursor_method, result_type):
    for row_id, value in enumerate((9, 9, 10, 11), 1):
        await JSONFields.objects.create(id=row_id, data=value)

    expected_ids = ([2, 3, 4], []) if cursor_method == "after_cursor" else ([], [1, 2, 3])
    expected = expected_ids
    if result_type == "count":
        expected = tuple(map(len, expected_ids))
    elif result_type == "exists":
        expected = tuple(map(bool, expected_ids))

    async def build(boundary):
        queryset = getattr(JSONFields.objects.order_by("data", "id"), cursor_method)(*boundary)
        if result_type == "values":
            return await queryset.values_list("id", flat=True)
        if result_type == "models":
            return [row.pk for row in await queryset]
        return await getattr(queryset, result_type)()

    await StatementPlanChecks.assert_a_later_query_runs_on_its_plan(
        build, ((9, 1), (11, 4)), expected_results=expected
    )


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
