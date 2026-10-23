"""The changes every write of a model with ``Meta.change_capture`` captures - one event per row it wrote, of the
operation it did to the row, whichever write it is."""

import pytest

from hare.exceptions import UnSupportedError
from hare.query.expressions import F
from tests.writes.rules.write_rules_models import RuleCapturedDocument, RuleCapturedPair, RuleOutboxEvent

INSERTED = "models.rulecaptureddocument.inserted"
UPDATED = "models.rulecaptureddocument.updated"
DELETED = "models.rulecaptureddocument.deleted"


async def create_object(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.create(id=2, title="b")


async def save_object(document: RuleCapturedDocument) -> None:
    document.title = "b"
    await document.save()


async def update_rows(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.filter(id=document.id).update(title="b")


async def bulk_update_objects(document: RuleCapturedDocument) -> None:
    document.title = "b"
    await RuleCapturedDocument.objects.bulk_update([document], ["title"])


async def bulk_create_objects(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.bulk_create([RuleCapturedDocument(id=2, title="b")])


async def upsert_objects(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.bulk_create(
        [RuleCapturedDocument(id=document.id, title="b"), RuleCapturedDocument(id=2, title="c")],
        update_fields=["title"],
        on_conflict=["id"],
    )


async def update_or_create_object(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.update_or_create(id=document.id, defaults={"title": "b"})


async def insert_from_rows(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.insert_from(
        RuleCapturedDocument.objects.annotate(new_id=F("id") + 1).values_list("new_id", "title"),
        fields=["id", "title"],
    )


async def delete_object(document: RuleCapturedDocument) -> None:
    await document.delete()


async def delete_rows(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.filter(id=document.id).delete()


@pytest.mark.parametrize(
    ("write", "expected_events"),
    [
        (create_object, [(INSERTED, 2)]),
        (save_object, [(UPDATED, 1)]),
        (update_rows, [(UPDATED, 1)]),
        (bulk_update_objects, [(UPDATED, 1)]),
        (bulk_create_objects, [(INSERTED, 2)]),
        (upsert_objects, [(INSERTED, 2), (UPDATED, 1)]),
        (update_or_create_object, [(UPDATED, 1)]),
        (insert_from_rows, [(INSERTED, 2)]),
        (delete_object, [(DELETED, 1)]),
        (delete_rows, [(DELETED, 1)]),
    ],
)
@pytest.mark.asyncio
async def test_every_write_captures_each_row_it_wrote(rules_db, write, expected_events):
    document = await RuleCapturedDocument.objects.create(id=1, title="a")
    await RuleOutboxEvent.objects.all().delete()
    await write(document)
    events = await RuleOutboxEvent.objects.all()
    assert sorted((event.topic, event.payload["pk"]) for event in events) == sorted(expected_events)


@pytest.mark.asyncio
async def test_an_update_moving_rows_to_other_keys_captures_them_as_they_were(rules_db):
    await RuleCapturedPair.objects.create(a=1, b=1, note="x")
    await RuleOutboxEvent.objects.all().delete()
    await RuleCapturedPair.objects.filter(a=1, b=1).update(b=5, note="y")
    (event,) = await RuleOutboxEvent.objects.all()
    assert (event.payload["before"], event.payload["after"]) == (
        {"a": 1, "b": 1, "note": "x"},
        {"a": 1, "b": 5, "note": "y"},
    )


@pytest.mark.asyncio
async def test_an_update_moving_rows_to_keys_an_expression_gives_needs_the_rows_as_they_were(rules_db):
    await RuleCapturedPair.objects.create(a=1, b=1, note="x")
    update = RuleCapturedPair.objects.filter(a=1).update(b=F("b") + 1)
    if RuleCapturedPair._meta.connection.features.supports_returning_old_new:
        assert await update == 1
        return
    with pytest.raises(UnSupportedError, match="sets 'b', a column of its primary key, from an expression"):
        await update
    assert await RuleCapturedPair.objects.values_list("a", "b") == [(1, 1)]
