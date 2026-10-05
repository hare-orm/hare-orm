"""Model.hard_delete(): a real DELETE even with Meta.soft_delete_field set."""

import pytest

from hare.exceptions import (
    ProtectedError,
    QueryError,
)
from tests.testmodels import (
    Author,
    SoftDeleteChildCascadeHard,
    SoftDeleteChildCascadeSoft,
    SoftDeleteChildProtect,
    SoftDeleteParent,
)


@pytest.mark.asyncio
async def test_hard_delete_removes_a_live_row_for_real(db):
    parent = await SoftDeleteParent.objects.create(name="live")

    await parent.hard_delete()

    assert not await SoftDeleteParent.objects.include_deleted().filter(id=parent.id).exists()


@pytest.mark.asyncio
async def test_hard_delete_empties_an_already_soft_deleted_row(db):
    parent = await SoftDeleteParent.objects.create(name="trashed")
    await parent.delete()
    trashed = await SoftDeleteParent.objects.only_deleted().get(id=parent.id)

    await trashed.hard_delete()

    assert not await SoftDeleteParent.objects.include_deleted().filter(id=parent.id).exists()


@pytest.mark.asyncio
async def test_hard_delete_removes_cascade_children_for_real(db):
    parent = await SoftDeleteParent.objects.create(name="parent")
    soft_child = await SoftDeleteChildCascadeSoft.objects.create(name="soft child", parent=parent)
    hard_child = await SoftDeleteChildCascadeHard.objects.create(name="hard child", parent=parent)

    await parent.hard_delete()

    assert not await SoftDeleteChildCascadeSoft.objects.include_deleted().filter(id=soft_child.id).exists()
    assert not await SoftDeleteChildCascadeHard.objects.filter(id=hard_child.id).exists()


@pytest.mark.asyncio
async def test_hard_delete_after_a_soft_delete_cascade_removes_the_trashed_children(db):
    parent = await SoftDeleteParent.objects.create(name="parent")
    soft_child = await SoftDeleteChildCascadeSoft.objects.create(name="soft child", parent=parent)
    await parent.delete()
    assert await SoftDeleteChildCascadeSoft.objects.only_deleted().filter(id=soft_child.id).exists()

    await (await SoftDeleteParent.objects.only_deleted().get(id=parent.id)).hard_delete()

    assert not await SoftDeleteChildCascadeSoft.objects.include_deleted().filter(id=soft_child.id).exists()


@pytest.mark.asyncio
async def test_hard_delete_respects_protect(db):
    parent = await SoftDeleteParent.objects.create(name="protected parent")
    await SoftDeleteChildProtect.objects.create(name="guard", parent=parent)

    with pytest.raises(ProtectedError):
        await parent.hard_delete()

    assert await SoftDeleteParent.objects.filter(id=parent.id).exists()


@pytest.mark.asyncio
async def test_hard_delete_on_a_model_without_soft_delete_is_a_plain_delete(db):
    author = await Author.objects.create(name="Plain")

    await author.hard_delete()

    assert not await Author.objects.filter(id=author.id).exists()


@pytest.mark.asyncio
async def test_hard_delete_of_an_unsaved_instance_is_refused(db):
    with pytest.raises(QueryError):
        await SoftDeleteParent(name="never saved").hard_delete()
