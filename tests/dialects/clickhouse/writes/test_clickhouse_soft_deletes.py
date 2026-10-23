"""A soft delete on ClickHouse - the rows it marks are written by a mutation, its uniqueness checked by
hare on the rows as the mutation leaves them."""

import pytest

from tests.dialects.clickhouse.models import ArchivedBook, ArchivedShelf


@pytest.mark.asyncio
async def test_a_soft_delete_cascades_to_soft_deleted_children(clickhouse_db):
    """A soft delete of a shelf soft-deletes its books."""
    await ArchivedShelf.objects.bulk_create([ArchivedShelf(id=1), ArchivedShelf(id=2)])
    await ArchivedBook.objects.bulk_create(
        [
            ArchivedBook(id=1, isbn="a", shelf_id=1),
            ArchivedBook(id=2, isbn="b", shelf_id=1),
            ArchivedBook(id=3, isbn="c", shelf_id=2),
        ]
    )

    await (await ArchivedShelf.objects.get(id=1)).delete()

    assert await ArchivedShelf.objects.values_list("id", flat=True) == [2]
    assert await ArchivedBook.objects.order_by("id").values_list("id", flat=True) == [3]
    assert await ArchivedBook.objects.only_deleted().order_by("id").values_list("id", flat=True) == [1, 2]
    await ArchivedBook.objects.filter(id=3).delete()
    assert await ArchivedBook.objects.count() == 0
