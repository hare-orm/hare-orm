"""A plain query (filters, ordering, .distinct() and a slice only) takes the short query-shape key."""

import pytest

from hare.core.caching.caches import Caches
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.statements.select.model_rows.instance_hydration import InstanceHydration
from tests import testmodels


def shape_keys(model):
    bucket = model._meta.plan_cache_buckets.get(id(StatementPlans.plans))
    return [] if bucket is None else [rest for _index, rest in bucket]


@pytest.mark.asyncio
async def test_plain_query_is_stored_under_the_short_key(db):
    StatementPlans.plans.clear()
    await testmodels.Tournament.objects.create(name="plain")
    await testmodels.Tournament.objects.filter(name="plain").order_by("id")
    keys = shape_keys(testmodels.Tournament)
    assert len(keys) == 1
    # query class, declaration, model, dialect, connection, visibility, columns, orderings,
    # Meta.ordering off, distinct, LIMIT present, OFFSET present, .none(), filters, CTEs, dialect
    # QuerySet method calls, zone - the model is the bucket too.
    assert len(keys[0]) == 17


@pytest.mark.asyncio
async def test_plain_and_full_shapes_of_the_same_filter_stay_apart(db):
    tournament = await testmodels.Tournament.objects.create(name="shape")
    await testmodels.Event.objects.create(name="first", tournament=tournament)
    await testmodels.Event.objects.create(name="second", tournament=tournament)
    for _ in range(2):
        plain = await testmodels.Event.objects.filter(tournament_id=tournament.id).order_by("name")
        joined = (
            await testmodels.Event.objects.filter(tournament_id=tournament.id)
            .select_related("tournament")
            .order_by("name")
        )
        partial = (
            await testmodels.Event.objects.filter(tournament_id=tournament.id)
            .only("event_id", "name")
            .order_by("-name")
        )
        distinct = await testmodels.Event.objects.filter(tournament_id=tournament.id).distinct().order_by("name")
        assert [event.name for event in plain] == ["first", "second"]
        assert [event.name for event in joined] == ["first", "second"]
        assert joined[0].tournament.name == "shape"
        assert [event.name for event in partial] == ["second", "first"]
        assert partial[0]._partial and not plain[0]._partial
        assert [event.name for event in distinct] == ["first", "second"]


@pytest.mark.asyncio
async def test_ordering_and_filter_values_change_between_hits(db):
    for name in ("a", "b", "c"):
        await testmodels.Tournament.objects.create(name=name)
    assert [t.name for t in await testmodels.Tournament.objects.filter(name__in=["a", "c"]).order_by("name")] == [
        "a",
        "c",
    ]
    assert [t.name for t in await testmodels.Tournament.objects.filter(name__in=["a", "b"]).order_by("-name")] == [
        "b",
        "a",
    ]
    assert [t.name for t in await testmodels.Tournament.objects.filter(name__in=["b", "c"]).order_by("name")[1:]] == [
        "c"
    ]
    assert await testmodels.Tournament.objects.filter(name="b").first() is not None


@pytest.mark.asyncio
async def test_forgetting_model_caches_drops_the_plain_shape_caches(db):
    await testmodels.Tournament.objects.filter(name="x")
    assert testmodels.Tournament in InstanceHydration.ALL_FIELDS_SELECTS_KEY_CACHE
    Caches.forget_model_caches([testmodels.Tournament])
    assert testmodels.Tournament not in InstanceHydration.ALL_FIELDS_SELECTS_KEY_CACHE
