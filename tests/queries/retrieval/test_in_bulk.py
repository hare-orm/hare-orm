"""``QuerySet.in_bulk()`` - the objects of a queryset by their value of a unique field."""

import pytest

from hare.exceptions import FieldError, QueryError
from tests.shared_models.testmodels import CompositePkThing, Tournament, UniqueName


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
