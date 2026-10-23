"""A name or a list of names given as something else is refused with the method's own error - never
an AttributeError of the value, and never a string read letter by letter as a list of names."""

import pytest

from hare.exceptions import QueryError
from tests.shared_models.testmodels import Tournament


@pytest.mark.parametrize(
    ("make", "error", "message"),
    [
        (lambda: Tournament.objects.distinct(1), QueryError, r"distinct\(\) takes field names, got 1"),
        (lambda: Tournament.objects.only(1), QueryError, r"only\(\) takes field names, got 1"),
        (lambda: Tournament.objects.values("name", 1), QueryError, r"values\(\) takes field names, got 1"),
        (lambda: Tournament.objects.values_list(None), QueryError, r"values_list\(\) takes field names, got None"),
    ],
)
@pytest.mark.asyncio
async def test_a_wrong_type_is_refused_with_the_methods_error(db, make, error, message):
    with pytest.raises(error, match=message):
        make()


@pytest.mark.parametrize("batch_size", [1.5, True, "10", 0, -1])
@pytest.mark.asyncio
async def test_a_batch_size_of_no_positive_int_is_refused_before_any_query(db, batch_size):
    with pytest.raises(QueryError, match=r"bulk_create\(\): batch_size must be a positive integer"):
        await Tournament.objects.bulk_create([Tournament(name="a")], batch_size=batch_size)
    with pytest.raises(QueryError, match=r"bulk_update\(\): batch_size must be a positive integer"):
        await Tournament.objects.bulk_update([], ["name"], batch_size=batch_size)
    assert await Tournament.objects.count() == 0


@pytest.mark.parametrize("batch_size", [10**9, 2**63, 10**30])
@pytest.mark.asyncio
async def test_a_batch_size_larger_than_the_objects_writes_them_in_one_batch(db, batch_size):
    """A batch was allocated at its full size up front - MemoryError for 2**63, gigabytes for 10**9."""
    await Tournament.objects.bulk_create([Tournament(name="a"), Tournament(name="b")], batch_size=batch_size)
    created = await Tournament.objects.all()
    for tournament in created:
        tournament.name += "!"
    await Tournament.objects.bulk_update(created, ["name"], batch_size=batch_size)
    assert sorted(await Tournament.objects.values_list("name", flat=True)) == ["a!", "b!"]


@pytest.mark.parametrize("value", [True, 5, float("nan"), object()])
@pytest.mark.asyncio
async def test_a_collection_argument_given_no_collection_is_refused(db, value):
    """Each raised a bare TypeError/AttributeError of the value - "'bool' object is not iterable",
    "'int' object has no attribute 'items'"."""
    tournament = await Tournament.objects.create(name="a")
    for make in (
        lambda: Tournament.objects.bulk_create(value),
        lambda: Tournament.objects.bulk_create([Tournament(name="b")], update_fields=value, on_conflict=["id"]),
        lambda: Tournament.objects.bulk_update(value, ["name"]),
        lambda: Tournament.objects.bulk_update([tournament], value),
        lambda: tournament.save(update_fields=value),
        lambda: tournament.refresh_from_db(fields=value),
        lambda: Tournament.objects.in_bulk(value),
    ):
        with pytest.raises(QueryError):
            await make()
