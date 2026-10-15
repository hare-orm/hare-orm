"""QuerySet.restore(cascade=False): the soft-deleted rows among a queryset's brought back - in one UPDATE,
with the cascade of their soft delete, through a model's own restore() - and what it refuses."""

from __future__ import annotations

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import QueryError
from hare.models.tenancy.tenancy import Tenancy
from tests.testmodels import (
    AuditedRestoreNote,
    DeletePreviewSoftFolder,
    DeletePreviewSoftNote,
    DeletePreviewTenantSoftProject,
    IntFields,
    SoftDeleteAutoNow,
    SoftDeleteVersionedDirtyTracked,
)


async def create_and_delete(model, names: list[str], deleted: list[str]) -> None:
    for name in names:
        await model.objects.create(name=name)
    await model.objects.filter(name__in=deleted).delete()


@pytest.mark.asyncio
async def test_the_deleted_rows_among_the_queryset_are_restored(db):
    await create_and_delete(SoftDeleteAutoNow, ["a", "b", "c", "d"], ["a", "b", "c"])
    async with capture_queries() as queries:
        restored = await SoftDeleteAutoNow.objects.filter(name__in=["a", "b", "d"]).restore()
    assert restored == 2
    assert queries.count == 1
    assert sorted(await SoftDeleteAutoNow.objects.values_list("name", flat=True)) == ["a", "b", "d"]
    assert await SoftDeleteAutoNow.objects.include_deleted().filter(name="c").restore() == 1
    assert await SoftDeleteAutoNow.objects.only_deleted().count() == 0
    assert await SoftDeleteAutoNow.objects.all().restore() == 0


@pytest.mark.asyncio
async def test_the_version_is_bumped(db):
    row = await SoftDeleteVersionedDirtyTracked.objects.create(name="a")
    await SoftDeleteVersionedDirtyTracked.objects.filter(id=row.id).delete()
    deleted_version = (await SoftDeleteVersionedDirtyTracked.objects.only_deleted().get(id=row.id)).version
    assert await SoftDeleteVersionedDirtyTracked.objects.filter(id=row.id).restore() == 1
    assert (await SoftDeleteVersionedDirtyTracked.objects.get(id=row.id)).version == deleted_version + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cascade", [False, True])
async def test_the_cascade_of_the_soft_delete(db, cascade):
    folder = await DeletePreviewSoftFolder.objects.create(name="folder")
    await DeletePreviewSoftNote.objects.create(name="cascaded", folder=folder)
    await DeletePreviewSoftFolder.objects.filter(id=folder.id).delete()
    assert await DeletePreviewSoftNote.objects.count() == 0
    assert await DeletePreviewSoftFolder.objects.filter(id=folder.id).restore(cascade=cascade) == 1
    assert await DeletePreviewSoftFolder.objects.count() == 1
    assert await DeletePreviewSoftNote.objects.count() == (1 if cascade else 0)


@pytest.mark.asyncio
async def test_a_model_overriding_restore(db):
    AuditedRestoreNote.restore_override_calls.clear()
    await create_and_delete(AuditedRestoreNote, ["a", "b", "c"], ["a", "b"])
    assert await AuditedRestoreNote.objects.all().restore() == 2
    assert sorted(AuditedRestoreNote.restore_override_calls) == [("a", False), ("b", False)]
    assert await AuditedRestoreNote.objects.count() == 3


@pytest.mark.asyncio
async def test_only_the_active_tenants_rows(db):
    for company_id in (1, 2):
        with Tenancy.scope(company_id):
            project = await DeletePreviewTenantSoftProject.objects.create(company_id=company_id)
            await DeletePreviewTenantSoftProject.objects.filter(id=project.id).delete()
    with Tenancy.scope(1):
        assert await DeletePreviewTenantSoftProject.objects.all().restore() == 1
    assert await DeletePreviewTenantSoftProject.objects.all_tenants().only_deleted().count() == 1
    assert await DeletePreviewTenantSoftProject.objects.all_tenants().restore(cascade=True) == 1


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: IntFields.objects.all().restore(), "has no Meta.soft_delete_field"),
        (lambda: SoftDeleteAutoNow.objects.all().restore(cascade="yes"), "takes a bool"),
        (lambda: SoftDeleteAutoNow.objects.values("name").restore(), "restore"),
        (
            lambda: SoftDeleteAutoNow.objects.filter(id=1).union(SoftDeleteAutoNow.objects.filter(id=2)).restore(),
            "restore\\(\\) can't be used on a union",
        ),
    ],
)
def test_a_wrong_restore_is_refused(make, message):
    with pytest.raises(QueryError, match=message):
        make()
