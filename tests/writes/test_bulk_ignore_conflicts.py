"""bulk_create(ignore_conflicts=True) gives the objects it inserted - told by the rows RETURNING gives back,
by the given keys or by on_conflict - and marks only those saved."""

import math

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import UnSupportedError
from hare.models.write.returned_values import ReturnedValues
from tests.shared_models.testmodels import CompositePkThing, UniqueName

REQUIRED_FEATURES = {"supports_unique_constraints": True, "supports_returning": True}


@requires_features(**REQUIRED_FEATURES)
@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [None, 1])
async def test_objects_with_keys_give_the_ones_inserted(db_truncate, batch_size):
    await UniqueName.objects.create(id=1, name="taken")
    repeated_key = UniqueName(id=1, name="other")
    repeated_name = UniqueName(id=2, name="taken")
    new = UniqueName(id=3, name="new")

    inserted = await UniqueName.objects.bulk_create(
        [repeated_key, repeated_name, new], batch_size=batch_size, ignore_conflicts=True
    )

    assert inserted == [new]
    assert [obj._saved_in_db for obj in (repeated_key, repeated_name, new)] == [False, False, True]
    assert await UniqueName.objects.order_by("id").values_list("id", "name") == [(1, "taken"), (3, "new")]


@requires_features(**REQUIRED_FEATURES)
@pytest.mark.asyncio
@pytest.mark.parametrize("batch_size", [None, 1])
async def test_objects_with_keys_the_database_generates_are_matched_by_on_conflict(db_truncate, batch_size):
    await UniqueName.objects.create(name="taken")
    first, skipped, second = UniqueName(name="first"), UniqueName(name="taken"), UniqueName(name="second")

    inserted = await UniqueName.objects.bulk_create(
        [first, skipped, second], batch_size=batch_size, ignore_conflicts=True, on_conflict=["name"]
    )

    assert inserted == [first, second]
    assert skipped.id is None
    assert not skipped._saved_in_db
    assert {first.id, second.id} == set(
        await UniqueName.objects.filter(name__in=["first", "second"]).values_list("id", flat=True)
    )


@requires_features(**REQUIRED_FEATURES)
@pytest.mark.asyncio
async def test_the_first_of_objects_sharing_a_conflict_key_is_the_one_inserted(db_truncate):
    first, repeated = UniqueName(name="same", optional="first"), UniqueName(name="same", optional="repeated")

    assert await UniqueName.objects.bulk_create([first, repeated], ignore_conflicts=True, on_conflict=["name"]) == [
        first
    ]
    assert await UniqueName.objects.values_list("optional", flat=True) == ["first"]


@requires_features(**REQUIRED_FEATURES)
@pytest.mark.asyncio
async def test_objects_without_a_value_of_the_conflict_target_are_all_inserted(db_truncate):
    first, second = UniqueName(name=None, optional="first"), UniqueName(name=None, optional="second")

    inserted = await UniqueName.objects.bulk_create([first, second], ignore_conflicts=True, on_conflict=["name"])

    assert inserted == [first, second]
    stored = dict(await UniqueName.objects.values_list("optional", "id"))
    assert (first.id, second.id) == (stored["first"], stored["second"])


@requires_features(**REQUIRED_FEATURES)
@pytest.mark.asyncio
async def test_objects_targeting_the_key_the_database_generates_are_all_given_back(db_truncate):
    objs = [UniqueName(name="a"), UniqueName(name="b")]

    inserted = await UniqueName.objects.bulk_create(objs, ignore_conflicts=True, on_conflict=["id"])

    assert inserted == objs
    assert [obj.id for obj in objs] == await UniqueName.objects.order_by("name").values_list("id", flat=True)


@pytest.mark.database_independent
def test_a_nan_of_a_key_is_equal_to_itself_as_a_unique_index_holds_it():
    assert ReturnedValues.get_hashable(math.nan) == ReturnedValues.get_hashable(float("nan"))
    assert ReturnedValues.get_hashable({"a": [1, {"b": math.nan}]}) == frozenset(
        {("a", (1, frozenset({("b", ReturnedValues.NAN_KEY)})))}
    )


@pytest.mark.database_independent
def test_a_mapping_of_a_key_is_equal_to_itself_in_any_order_of_its_keys():
    """The database gives an hstore's or a jsonb's keys back in an order of its own - the row of one object
    was matched to another, equal one written with its keys in another order."""
    assert ReturnedValues.get_hashable({"a": "1", "b": {"c": 1, "d": [2]}}) == ReturnedValues.get_hashable(
        {"b": {"d": [2], "c": 1}, "a": "1"}
    )
    assert ReturnedValues.get_hashable([1, 2]) != ReturnedValues.get_hashable([2, 1])


@requires_features(**REQUIRED_FEATURES)
@pytest.mark.asyncio
async def test_a_composite_key_tells_the_objects_inserted(db_truncate):
    await CompositePkThing.objects.bulk_create([CompositePkThing(thing_id=1, revision=1, name="old")])
    repeated, new = (
        CompositePkThing(thing_id=1, revision=1, name="x"),
        CompositePkThing(thing_id=1, revision=2, name="y"),
    )

    assert await CompositePkThing.objects.bulk_create([repeated, new], ignore_conflicts=True) == [new]


@requires_features(**REQUIRED_FEATURES)
@pytest.mark.asyncio
async def test_keys_the_database_generates_without_on_conflict_are_refused_before_writing(db_truncate):
    with pytest.raises(UnSupportedError, match="on_conflict"):
        await UniqueName.objects.bulk_create([UniqueName(name="a")], ignore_conflicts=True)
    assert await UniqueName.objects.count() == 0


@requires_features(**REQUIRED_FEATURES)
@pytest.mark.asyncio
async def test_no_conflict_gives_every_object(db_truncate):
    objects = [UniqueName(name="a"), UniqueName(name="b")]

    assert await UniqueName.objects.bulk_create(objects, ignore_conflicts=True, on_conflict=["name"]) == objects
    assert all(obj._saved_in_db and obj.id is not None for obj in objects)
