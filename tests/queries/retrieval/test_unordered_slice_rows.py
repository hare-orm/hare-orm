"""An unordered slice holds the first rows by primary key - for first(), last() and aggregate() it did,
but get(), latest() and earliest() of it took the rows of the slice in the database's own order: on
PostgreSQL, once rows were updated, a row outside the slice."""

import pytest

from tests.shared_models import testmodels


@pytest.mark.asyncio
async def test_a_single_row_of_an_unordered_slice_is_one_of_its_first_rows_by_primary_key(db):
    for number in range(1, 8):
        await testmodels.IntFields.objects.create(id=number, intnum=number)
    # Rewritten rows move to the end of a PostgreSQL table's own order.
    await testmodels.IntFields.objects.filter(id__lte=4).update(intnum=0)
    sliced = testmodels.IntFields.objects.all()[2:5]

    assert (await sliced.latest("id")).id == 5
    assert (await sliced.earliest("id")).id == 3
    assert (await testmodels.IntFields.objects.all()[2:3].get()).id == 3
    assert (await sliced.first()).id == 3
    assert (await sliced.last()).id == 5
