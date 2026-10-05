"""Change capture of the writes of a queryset - update(), delete(), insert_from() - on ClickHouse, whose
writes return no rows: the rows are read before the write and, for an update or an insert, after it."""

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.exceptions import UnSupportedError
from tests.dialects.clickhouse.change_capture.models import ArchivedNote, CapturedNote, CaptureOutbox


async def get_events():
    events = await CaptureOutbox.objects.all()
    return sorted((event.topic, event.payload["pk"], event.payload.get("after")) for event in events)


async def create_notes():
    await CapturedNote.objects.bulk_create([CapturedNote(id=note_id, text=f"n{note_id}") for note_id in (1, 2, 3)])
    await CaptureOutbox.objects.all().delete()


@pytest.mark.asyncio
async def test_an_update_captures_each_row_it_wrote(clickhouse_change_capture_db):
    await create_notes()
    assert await CapturedNote.objects.filter(id__in=[1, 2]).update(text="z") == 2
    assert await get_events() == [
        ("models.capturednote.updated", 1, {"id": 1, "text": "z"}),
        ("models.capturednote.updated", 2, {"id": 2, "text": "z"}),
    ]


@pytest.mark.asyncio
async def test_a_delete_captures_each_row_as_it_was(clickhouse_change_capture_db):
    await create_notes()
    await CapturedNote.objects.filter(id=3).delete()
    events = await CaptureOutbox.objects.all()
    assert [(event.topic, event.payload["pk"], event.payload["before"]) for event in events] == [
        ("models.capturednote.deleted", 3, {"id": 3, "text": "n3"})
    ]
    assert await CapturedNote.objects.order_by("id").values_list("id", flat=True) == [1, 2]


@pytest.mark.asyncio
async def test_an_insert_from_a_queryset_captures_the_rows_it_wrote(clickhouse_change_capture_db):
    await create_notes()
    assert (
        await ArchivedNote.objects.insert_from(CapturedNote.objects.values("id", "text"), fields=["id", "text"]) == 3
    )
    assert await get_events() == [
        ("models.archivednote.inserted", note_id, {"id": note_id, "text": f"n{note_id}"}) for note_id in (1, 2, 3)
    ]


@pytest.mark.asyncio
async def test_an_insert_from_writing_no_key_is_refused(clickhouse_change_capture_db):
    await create_notes()
    with pytest.raises(UnSupportedError, match="name the key fields"):
        await ArchivedNote.objects.insert_from(CapturedNote.objects.values("text"), fields=["text"])
    assert await ArchivedNote.objects.count() == 0


@pytest.mark.asyncio
async def test_a_relay_is_refused(clickhouse_change_capture_db):
    async def deliver(event):
        return None

    with pytest.raises(UnSupportedError, match="UPDATE ... RETURNING"):
        await OutboxRelay(CaptureOutbox, deliver).poll_once()
