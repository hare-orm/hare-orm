"""Rows keyed by two columns on ClickHouse: read, saved, bulk-updated and deleted by their key."""

import pytest

from tests.dialects.clickhouse.models import Revision


@pytest.mark.asyncio
async def test_rows_of_a_composite_key_are_written_and_read_by_it(clickhouse_db):
    await Revision.objects.bulk_create(
        [
            Revision(document_id=document, number=number, title=f"{document}.{number}")
            for document in (1, 2)
            for number in (1, 2)
        ]
    )
    revision = await Revision.objects.get(pk=(2, 1))
    assert revision.title == "2.1"
    revision.title = "changed"
    await revision.save()
    assert await Revision.objects.filter(pk__in=[(2, 1), (1, 2)]).order_by("document_id").values_list(
        "title", flat=True
    ) == ["1.2", "changed"]


@pytest.mark.asyncio
async def test_bulk_update_matches_both_key_columns(clickhouse_db):
    await Revision.objects.bulk_create(
        [Revision(document_id=document, number=number, title="t") for document in (1, 2) for number in (1, 2)]
    )
    revisions = [await Revision.objects.get(pk=(1, 2)), await Revision.objects.get(pk=(2, 1))]
    for revision in revisions:
        revision.score = revision.document_id * 10 + revision.number
    await Revision.objects.bulk_update(revisions, ["score"])
    assert await Revision.objects.order_by("document_id", "number").values_list("document_id", "number", "score") == [
        (1, 1, None),
        (1, 2, 12.0),
        (2, 1, 21.0),
        (2, 2, None),
    ]
    await revisions[0].delete()
    assert await Revision.objects.count() == 3
