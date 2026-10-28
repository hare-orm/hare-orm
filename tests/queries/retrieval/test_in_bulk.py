"""``QuerySet.in_bulk()`` - the objects of a queryset by their value of a unique field."""

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import FieldError, QueryError
from tests.shared_models.testmodels import CompositePkThing, Event, Tournament, UniqueName


@pytest.mark.asyncio
async def test_objects_by_primary_key(db_truncate):
    first = await Tournament.objects.create(id=1, name="a")
    await Tournament.objects.create(id=2, name="b")
    third = await Tournament.objects.create(id=3, name="c")
    by_key = await Tournament.objects.in_bulk([1, 3, 99])
    assert sorted(by_key) == [1, 3]
    assert (by_key[1].name, by_key[3].name) == (first.name, third.name)
    assert sorted(await Tournament.objects.in_bulk()) == [1, 2, 3]
    assert await Tournament.objects.in_bulk([]) == {}
    assert sorted(await Tournament.objects.filter(name__in=["a", "b"]).in_bulk()) == [1, 2]
    assert sorted(await Tournament.objects.in_bulk(key for key in (2, 3))) == [2, 3]


@pytest.mark.asyncio
async def test_objects_by_unique_field(db_truncate):
    await UniqueName.objects.bulk_create([UniqueName(name="a"), UniqueName(name="b")])
    by_name = await UniqueName.objects.in_bulk(["b"], field_name="name")
    assert list(by_name) == ["b"]
    assert by_name["b"].name == "b"
    with pytest.raises(QueryError, match="unique field"):
        await UniqueName.objects.in_bulk(["x"], field_name="optional")
    with pytest.raises(FieldError):
        await UniqueName.objects.in_bulk(["x"], field_name="missing")


@pytest.mark.asyncio
async def test_objects_by_composite_primary_key(db_truncate):
    await CompositePkThing.objects.bulk_create(
        [CompositePkThing(thing_id=1, revision=1, name="a"), CompositePkThing(thing_id=1, revision=2, name="b")]
    )
    by_key = await CompositePkThing.objects.in_bulk([(1, 2), (5, 5)])
    assert list(by_key) == [(1, 2)]
    assert by_key[(1, 2)].name == "b"
    assert sorted(await CompositePkThing.objects.in_bulk()) == [(1, 1), (1, 2)]


@pytest.mark.asyncio
async def test_a_sliced_or_values_queryset_is_refused(db_truncate):
    with pytest.raises(QueryError, match="sliced"):
        await Tournament.objects.limit(1).in_bulk()
    with pytest.raises(QueryError):
        await Tournament.objects.values("id").in_bulk()


@pytest.mark.asyncio
async def test_the_key_is_read_beside_the_fields_only_and_defer_choose(db_truncate):
    """The key wasn't loaded - the objects came from the database and reading their key raised
    NoValuesFetched."""
    await UniqueName.objects.bulk_create([UniqueName(id=1, name="a"), UniqueName(id=2, name="b")])
    by_key = await UniqueName.objects.only("optional").in_bulk([1])
    assert list(by_key) == [1]
    assert by_key[1]._partial
    assert list(await UniqueName.objects.defer("id").in_bulk([2])) == [2]
    by_name = await UniqueName.objects.only("id", "optional").in_bulk(["b"], field_name="name")
    assert list(by_name) == ["b"]
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="first")
    assert list(await CompositePkThing.objects.only("name").in_bulk([(1, 2)])) == [(1, 2)]


@requires_features(supports_distinct_on=True)
@pytest.mark.asyncio
async def test_a_relation_of_distinct_is_its_key_however_named(db_truncate):
    """distinct("tournament") beside field_name="tournament_id" was taken for another field: "in_bulk()'s
    field_name must be a unique field"."""
    first = await Tournament.objects.create(id=1, name="a")
    second = await Tournament.objects.create(id=2, name="b")
    for event_id, tournament in ((1, first), (2, first), (3, second)):
        await Event.objects.create(event_id=event_id, name=f"e{event_id}", tournament=tournament)
    for distinct_name, key_name in (("tournament", "tournament_id"), ("tournament_id", "tournament")):
        events = Event.objects.distinct(distinct_name).order_by(distinct_name, "event_id")
        by_tournament = await events.in_bulk(field_name=key_name)
        assert {key: event.event_id for key, event in by_tournament.items()} == {1: 1, 2: 3}
