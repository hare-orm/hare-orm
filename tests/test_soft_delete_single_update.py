"""The soft delete of a row no relation points at is one UPDATE of the row while it's live; a row it
doesn't match ends as the full delete ends it."""

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import IntegrityError, StaleObjectError
from tests.testmodels import SoftDeleteStandalone, SoftDeleteVersioned


@pytest.mark.asyncio
async def test_one_update(db):
    note = await SoftDeleteStandalone.objects.create(name="a")
    async with capture_queries() as counter:
        await note.delete()
    assert counter.count == 1
    assert note.deleted_at is not None
    stored = await SoftDeleteStandalone.objects.only_deleted().get(pk=note.pk)
    assert stored.deleted_at == note.deleted_at


@pytest.mark.asyncio
async def test_a_row_soft_deleted_meanwhile_keeps_its_deletion_time(db):
    note = await SoftDeleteStandalone.objects.create(name="a")
    first_copy = await SoftDeleteStandalone.objects.get(pk=note.pk)
    second_copy = await SoftDeleteStandalone.objects.get(pk=note.pk)
    await first_copy.delete()
    await second_copy.delete()
    stored = await SoftDeleteStandalone.objects.only_deleted().get(pk=note.pk)
    assert stored.deleted_at == first_copy.deleted_at
    assert second_copy.deleted_at == first_copy.deleted_at


@pytest.mark.asyncio
async def test_a_row_gone_meanwhile_raises(db):
    note = await SoftDeleteStandalone.objects.create(name="a")
    stale_copy = await SoftDeleteStandalone.objects.get(pk=note.pk)
    await note.hard_delete()
    with pytest.raises(IntegrityError):
        await stale_copy.delete()
    assert stale_copy.deleted_at is None


@pytest.mark.asyncio
async def test_a_stale_version_raises(db):
    document = await SoftDeleteVersioned.objects.create(name="a")
    stale_copy = await SoftDeleteVersioned.objects.get(pk=document.pk)
    document.name = "b"
    await document.save()
    with pytest.raises(StaleObjectError):
        await stale_copy.delete()
    assert stale_copy.deleted_at is None
    assert (await SoftDeleteVersioned.objects.get(pk=document.pk)).deleted_at is None
