"""PostgreSQL expressions keep a statement plan like the built-in ones - a pgvector distance, a
PostGIS distance or radius, a full-text search vector, query, rank and headline: a later query of
the same structure runs on the plan with its own vector, point or search text bound. Each test
compares a plan hit with the same query built in full."""

from unittest.mock import patch

import pytest

from hare.contrib.test import requires_features
from hare.dialects.postgresql.functions.gis import STDistance, STDWithin
from hare.dialects.postgresql.functions.vector import CosineDistance, L2Distance
from hare.dialects.postgresql.search import Lexeme, SearchHeadline, SearchQuery, SearchRank, SearchVector
from hare.query.expressions import Case, When
from hare.query.plans.statement_plans import StatementPlans
from hare.query.statements.awaitable_query import AwaitableQuery
from tests.dialects.postgresql.models_postgis import Place
from tests.dialects.postgresql.models_vector import VectorEntry
from tests.testmodels import TextFields


def full_build():
    """Runs a query built in full, as it ran before plans."""
    return patch.object(AwaitableQuery, "_run_on_plan", lambda self, *args, **kwargs: False)


async def assert_runs_on_plan(build, first_value, second_value) -> None:
    first = await build(first_value)
    with full_build():
        expected = await build(second_value)
    assert expected != first
    hits = StatementPlans.hits
    assert await build(second_value) == expected
    assert StatementPlans.hits - hits == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("distance_class", [L2Distance, CosineDistance])
async def test_a_vector_distance_runs_on_its_plan(db_vector, distance_class):
    await VectorEntry.objects.create(name="x", embedding=[1.0, 0.0, 0.0])
    await VectorEntry.objects.create(name="y", embedding=[0.0, 1.0, 0.0])
    await VectorEntry.objects.create(name="z", embedding=[0.0, 0.0, 1.0])

    def nearest(query_vector):
        return (
            VectorEntry.objects.annotate(dist=distance_class("embedding", query_vector))
            .order_by("dist", "id")
            .limit(2)
            .values_list("name", flat=True)
        )

    await assert_runs_on_plan(nearest, [1.0, 0.1, 0.0], [0.0, 0.1, 1.0])


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_geography_distance_and_radius_run_on_their_plans(db_postgis):
    await Place.objects.create(name="Moscow", location=(55.7558, 37.6173))
    await Place.objects.create(name="Kyiv", location=(50.45, 30.52))

    def nearest(point):
        return (
            Place.objects.annotate(dist=STDistance("location", point)).order_by("dist").values_list("name", flat=True)
        )

    await assert_runs_on_plan(nearest, (55.7, 37.6), (50.4, 30.5))

    def near(radius_m):
        return (
            Place.objects.annotate(near=STDWithin("location", (55.7558, 37.6173), radius_m))
            .filter(near=True)
            .order_by("name")
            .values_list("name", flat=True)
        )

    await assert_runs_on_plan(near, 1_000, 2_000_000)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_full_text_search_expressions_run_on_their_plans(db_postgres):
    await TextFields.objects.create(text="The quick brown fox jumps")
    await TextFields.objects.create(text="A lazy dog sleeps all day")

    def ranked(search_text):
        return (
            TextFields.objects.annotate(
                rank=SearchRank(SearchVector("text", config="english"), SearchQuery(search_text))
            )
            .order_by("-rank", "id")
            .values_list("text", flat=True)
        )

    await assert_runs_on_plan(ranked, "fox", "dog")

    def headlines(search_text):
        return (
            TextFields.objects.annotate(
                headline=SearchHeadline("text", SearchQuery(search_text), start_sel="[", stop_sel="]")
            )
            .order_by("id")
            .values_list("headline", flat=True)
        )

    await assert_runs_on_plan(headlines, "fox", "dog")

    def raw_lexemes(word):
        return (
            TextFields.objects.annotate(rank=SearchRank(SearchVector("text"), SearchQuery(Lexeme(word, prefix=True))))
            .order_by("-rank", "id")
            .values_list("text", flat=True)
        )

    await assert_runs_on_plan(raw_lexemes, "qui", "laz")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_distinct_on_an_annotation_holding_a_literal_binds_it(db_postgres):
    """DISTINCT ON an annotation renders its full expression again - its literal is bound there
    too, never kept from the first query of the plan."""
    for text in ("a", "b", "c", "d"):
        await TextFields.objects.create(text=text)

    def firsts(boundary):
        return (
            TextFields.objects.annotate(bucket=Case(When(text__lte=boundary, then=0), default=1))
            .order_by("bucket", "id")
            .distinct("bucket")
            .values_list("text", flat=True)
        )

    await assert_runs_on_plan(firsts, "a", "b")
    assert sorted(await firsts("c")) == ["a", "d"]
