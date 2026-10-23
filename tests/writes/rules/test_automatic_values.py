"""The values every write sets itself: the ``auto_now`` stamp - one moment per call - and the version, bumped
once by every update, whichever write it is."""

import datetime

import pytest

from hare.models.write.instance_values import InstanceValues
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from tests.writes.rules.write_rules_models import RuleDocument


async def save_document(document: RuleDocument) -> None:
    document.title = "b"
    await document.save()


async def save_some_fields(document: RuleDocument) -> None:
    document.title = "b"
    await document.save(update_fields=["title"])


async def update_rows(document: RuleDocument) -> None:
    await RuleDocument.objects.filter(id=document.id).update(title="b")


async def bulk_update_objects(document: RuleDocument) -> None:
    document.title = "b"
    await RuleDocument.objects.bulk_update([document], ["title"])


async def upsert_objects(document: RuleDocument) -> None:
    await RuleDocument.objects.bulk_create(
        [RuleDocument(id=document.id, title="b")], update_fields=["title"], on_conflict=["id"]
    )


async def delete_softly(document: RuleDocument) -> None:
    await document.delete()


@pytest.mark.parametrize(
    "write",
    [
        save_document,
        save_some_fields,
        update_rows,
        bulk_update_objects,
        upsert_objects,
        delete_softly,
    ],
)
@pytest.mark.asyncio
async def test_every_update_stamps_auto_now_and_bumps_the_version_once(rules_db, write):
    document = await RuleDocument.objects.create(id=1, title="a")
    stored = await RuleDocument.objects.include_deleted().get(id=1)
    await write(document)
    written = await RuleDocument.objects.include_deleted().get(id=1)
    assert written.version == stored.version + 1
    assert written.updated_at > stored.updated_at
    assert written.created_at == stored.created_at


@pytest.mark.asyncio
async def test_one_bulk_create_stamps_every_object_with_one_moment(rules_db):
    documents = [RuleDocument(id=document_id) for document_id in range(1, 6)]
    await RuleDocument.objects.bulk_create(documents)
    assert len({(document.created_at, document.updated_at) for document in documents}) == 1
    assert documents[0].created_at == documents[0].updated_at


@pytest.mark.asyncio
async def test_an_insert_keeps_an_auto_now_add_value_given(rules_db):
    document = await RuleDocument.objects.create(id=1)
    copied = RuleDocument(id=2, created_at=document.created_at)
    await copied.save()
    assert (await RuleDocument.objects.get(id=2)).created_at == document.created_at


@pytest.mark.asyncio
async def test_a_failed_insert_puts_the_stamps_back(rules_db):
    await RuleDocument.objects.create(id=1)
    duplicate = RuleDocument(id=1)
    with pytest.raises(Exception):
        await duplicate.save(force_create=True)
    assert duplicate.created_at is None
    assert duplicate.updated_at is None


@pytest.mark.parametrize("native", [True, False], ids=["native", "python"])
@pytest.mark.asyncio
async def test_a_stamp_sets_each_field_as_its_rule_says_and_a_failed_write_puts_them_back(
    rules_db, monkeypatch, native
):
    if not native:
        monkeypatch.setattr(RowWriteRules, "native_rows", None)
    rules = RowWriteRules.get(RuleDocument, WriteType.INSERT, None, "test")
    created_at = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
    given, unset = RuleDocument(id=1, created_at=created_at), RuleDocument(id=2)
    changed_values = InstanceValues()
    rules.stamp([given, unset], changed_values)
    assert given.created_at == created_at
    assert unset.created_at is not None
    assert given.updated_at == unset.updated_at == unset.created_at
    changed_values.restore()
    assert given.created_at == created_at
    assert unset.created_at is None
    assert given.updated_at is None
