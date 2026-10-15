"""Set operations keep a plan of the whole combined statement: each branch's shape is part of the key
and the branches' values, the slice included, are bound per query. Each test runs its queries at
least twice, so both the build and the plan hit return the same rows."""

import pytest

from hare.query.expressions import Value
from hare.query.plans.statement.statement_plans import StatementPlans
from tests.testmodels import Event, Tournament


async def count_plan_hits(statement) -> tuple[object, int]:
    hits = StatementPlans.hits
    result = await statement
    return result, StatementPlans.hits - hits


async def create_tournaments() -> list[Tournament]:
    tournaments = [await Tournament.objects.create(name=name) for name in ("a", "b", "c", "d")]
    for tournament in tournaments:
        await Event.objects.create(name=f"{tournament.name}-event", tournament=tournament)
    return tournaments


def names_of(first: str, second: str):
    return (
        Tournament.objects.filter(name=first)
        .values_list("name", flat=True)
        .union(Tournament.objects.filter(name=second).values_list("name", flat=True))
        .order_by("name")
    )


@pytest.mark.asyncio
async def test_values_union_runs_on_its_plan(db):
    await create_tournaments()
    assert await names_of("a", "b") == ["a", "b"]
    names, hits = await count_plan_hits(names_of("c", "d"))
    assert names == ["c", "d"]
    assert hits == 1
    assert await names_of("a", "nobody") == ["a"]


@pytest.mark.asyncio
async def test_values_union_slice_is_bound_per_query(db):
    await create_tournaments()
    everything = (
        Tournament.objects.filter(name__in=["a", "b"])
        .values_list("name", flat=True)
        .union(Tournament.objects.filter(name__in=["c", "d"]).values_list("name", flat=True))
    )
    ordered = everything.order_by("name")
    for start, expected in ((0, ["a", "b"]), (1, ["b", "c"]), (2, ["c", "d"]), (0, ["a", "b"])):
        assert await ordered[start : start + 2] == expected
    for limit in (1, 3, 2):
        assert await ordered.limit(limit) == ["a", "b", "c", "d"][:limit]


@pytest.mark.asyncio
async def test_values_union_of_sliced_branches_and_nested_operations(db):
    await create_tournaments()

    def query(first: str, second: str, third: str):
        return (
            Tournament.objects.filter(name=first)
            .values_list("name", flat=True)
            .union(
                Tournament.objects.filter(name=second)
                .values_list("name", flat=True)
                .union(Tournament.objects.filter(name=third).values_list("name", flat=True))
            )
            .order_by("name")
        )

    for first, second, third in (("a", "b", "c"), ("b", "c", "d"), ("a", "c", "d")):
        assert await query(first, second, third) == sorted({first, second, third})
    for start in (0, 1, 2):
        branch = Tournament.objects.all().order_by("name").values_list("name", flat=True)[start : start + 1]
        combined = branch.union(Tournament.objects.filter(name="d").values_list("name", flat=True)).order_by("name")
        assert await combined == sorted({["a", "b", "c"][start], "d"})


@pytest.mark.asyncio
async def test_values_union_count_and_exists(db):
    await create_tournaments()
    for first, second, expected in (("a", "b", 2), ("a", "nobody", 1), ("nobody", "nobody", 0)):
        union = names_of(first, second)
        assert await union.count() == expected
        assert await union.exists() is bool(expected)


@pytest.mark.asyncio
async def test_values_union_as_a_subquery(db):
    await create_tournaments()
    for first, second in (("a", "b"), ("c", "d")):
        ids = (
            Tournament.objects.filter(name=first)
            .values_list("id", flat=True)
            .union(Tournament.objects.filter(name=second).values_list("id", flat=True))
        )
        rows = await Event.objects.filter(tournament_id__in=ids).order_by("name")
        assert [event.name for event in rows] == [f"{first}-event", f"{second}-event"]


@pytest.mark.asyncio
async def test_sql_of_a_values_union_holds_its_own_values(db):
    await create_tournaments()
    for first, second in (("a", "b"), ("c", "d")):
        await names_of(first, second)
    sql = names_of("zzz", "yyy").sql(parameters_inline=True)
    assert "zzz" in sql and "yyy" in sql


def tournaments_named(first: str, second: str):
    return Tournament.objects.filter(name=first).union(Tournament.objects.filter(name=second)).order_by("name")


@pytest.mark.asyncio
async def test_model_union_runs_on_its_plan(db):
    await create_tournaments()
    assert [tournament.name for tournament in await tournaments_named("a", "b")] == ["a", "b"]
    rows, hits = await count_plan_hits(tournaments_named("c", "d"))
    assert [tournament.name for tournament in rows] == ["c", "d"]
    assert hits == 1
    assert all(isinstance(tournament, Tournament) and tournament._saved_in_db for tournament in rows)


@pytest.mark.asyncio
async def test_model_union_slice_single_row_and_annotations(db):
    await create_tournaments()
    union = (
        Tournament.objects.filter(name__in=["a", "b"])
        .union(Tournament.objects.filter(name__in=["c", "d"]))
        .order_by("name")
    )
    for start in (0, 1, 2, 0):
        assert [tournament.name for tournament in await union[start : start + 2]] == ["a", "b", "c", "d"][
            start : start + 2
        ]
    for name in ("a", "c"):
        assert (await tournaments_named(name, "nobody").first()).name == name
    for label in ("x", "y"):
        annotated = (
            Tournament.objects.filter(name="a")
            .annotate(label=Value(label))
            .union(Tournament.objects.filter(name="b").annotate(label=Value(label)))
            .order_by("name")
        )
        assert [(tournament.name, tournament.label) for tournament in await annotated] == [("a", label), ("b", label)]


@pytest.mark.asyncio
async def test_model_union_count_exists_and_nested(db):
    await create_tournaments()
    for first, second, expected in (("a", "b", 2), ("a", "nobody", 1), ("nobody", "nobody", 0)):
        union = Tournament.objects.filter(name=first).union(Tournament.objects.filter(name=second))
        assert await union.count() == expected
        assert await union.exists() is bool(expected)
    for first, second, third in (("a", "b", "c"), ("b", "c", "d")):
        nested = Tournament.objects.filter(name=first).union(
            Tournament.objects.filter(name=second).union(Tournament.objects.filter(name=third))
        )
        assert sorted(tournament.name for tournament in await nested) == [first, second, third]


@pytest.mark.asyncio
async def test_sql_of_a_model_union_holds_its_own_values(db):
    await create_tournaments()
    for first, second in (("a", "b"), ("c", "d")):
        await tournaments_named(first, second)
    sql = tournaments_named("zzz", "yyy").sql(parameters_inline=True)
    assert "zzz" in sql and "yyy" in sql
