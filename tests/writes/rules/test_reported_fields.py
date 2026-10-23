"""The fields a write reports as changed - the ones its caller named, as their fields holding columns, without the
``auto_now`` stamps, blind indexes and version the write adds itself - whichever write it is."""

import pytest

from hare.contrib.test import requires_features
from hare.instrumentation.declarations import RowsChanged
from hare.instrumentation.observers.observers import Observers
from hare.query.expressions import F
from tests.writes.rules.write_rules_models import (
    RuleCapturedDocument,
    RuleDocument,
    RuleDocumentTag,
    RuleOutboxEvent,
    RuleTag,
)


async def update_rows(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.filter(id=document.id).update(title="b")


async def save_some_fields(document: RuleCapturedDocument) -> None:
    document.title = "b"
    await document.save(update_fields=["title"])


async def bulk_update_objects(document: RuleCapturedDocument) -> None:
    document.title = "b"
    await RuleCapturedDocument.objects.bulk_update([document], ["title"])


async def upsert_objects(document: RuleCapturedDocument) -> None:
    await RuleCapturedDocument.objects.bulk_create(
        [RuleCapturedDocument(id=document.id, title="b")], update_fields=["title"], on_conflict=["id"]
    )


@pytest.mark.parametrize("write", [update_rows, save_some_fields, bulk_update_objects, upsert_objects])
@pytest.mark.asyncio
async def test_every_update_reports_the_fields_its_caller_named(rules_db, write):
    document = await RuleCapturedDocument.objects.create(id=1, title="a")
    await RuleOutboxEvent.objects.all().delete()
    await write(document)
    events = await RuleOutboxEvent.objects.all()
    assert [event.payload["changed"] for event in events] == [["title"]]


async def clear_rows() -> None:
    for model in (RuleDocumentTag, RuleTag, RuleDocument, RuleCapturedDocument, RuleOutboxEvent):
        await model.objects.all().delete()


@requires_features(supports_merge=True, supports_merge_returning=True)
@pytest.mark.asyncio
async def test_a_merge_reports_the_fields_its_update_branch_sets(rules_context):
    """Its events come once the write commits - observed outside the rolled-back transaction of the other tests."""
    try:
        await RuleCapturedDocument.objects.create(id=1, title="a")
        await RuleOutboxEvent.objects.all().delete()
        observed: list[RowsChanged] = []
        with Observers.observing(RowsChanged, observed.append, models=[RuleCapturedDocument]):
            await RuleCapturedDocument.objects.merge([{"id": 1, "title": "b"}], on="id").when_matched(
                update={"title": F("merge_source__title")}
            )
        events = await RuleOutboxEvent.objects.all()
        assert [event.payload["changed"] for event in events] == [["title"]]
        assert [event.fields for event in observed] == [("title",)]
    finally:
        await clear_rows()


@pytest.mark.asyncio
async def test_links_left_as_they_were_report_nothing(rules_context):
    try:
        document = await RuleDocument.objects.create(id=1, title="a")
        unlinked = await RuleDocument.objects.create(id=2, title="b")
        tag = await RuleTag.objects.create(id=1, name="t")
        other = await RuleTag.objects.create(id=2, name="u")
        await document.tags.add(tag)
        observed: list[RowsChanged] = []
        with Observers.observing(RowsChanged, observed.append):
            await document.tags.add(tag)
            await document.tags.remove(other)
            await unlinked.tags.clear()
        assert observed == []
        with Observers.observing(RowsChanged, observed.append):
            await document.tags.add(other)
        assert observed
        observed.clear()
        third = await RuleTag.objects.create(id=3, name="v")
        with Observers.observing(RowsChanged, observed.append, models=[RuleTag]):
            await document.tags.remove(third, other)
        assert [event.pks for event in observed] == [(2,)]
    finally:
        await clear_rows()
