"""A forward FK to a soft-deleted row: relation-level lookups see no related row, like reading it."""

import pytest

from hare.query.expressions import Q
from tests.testmodels import SoftDeleteChildCascadeSoft, SoftDeleteGhostChild, SoftDeleteParent, SoftDeleteVersioned


async def create_children() -> tuple[SoftDeleteGhostChild, SoftDeleteGhostChild, SoftDeleteGhostChild]:
    deleted_parent = await SoftDeleteVersioned.objects.create(name="deleted")
    await deleted_parent.delete()
    live_parent = await SoftDeleteVersioned.objects.create(name="live")
    orphan = await SoftDeleteGhostChild.objects.create(name="orphan", parent=deleted_parent)
    linked = await SoftDeleteGhostChild.objects.create(name="linked", parent=live_parent)
    unlinked = await SoftDeleteGhostChild.objects.create(name="unlinked")
    return orphan, linked, unlinked


async def names(queryset) -> list[str]:
    return sorted(await queryset.values_list("name", flat=True))


@pytest.mark.asyncio
async def test_isnull_true_finds_a_row_whose_target_is_soft_deleted(db):
    orphan, _, _ = await create_children()

    assert await names(SoftDeleteGhostChild.objects.filter(parent__isnull=True)) == ["orphan", "unlinked"]
    assert await names(SoftDeleteGhostChild.objects.filter(parent=None)) == ["orphan", "unlinked"]
    assert await names(SoftDeleteGhostChild.objects.filter(parent__not_isnull=False)) == ["orphan", "unlinked"]
    reloaded_orphan = await SoftDeleteGhostChild.objects.filter(pk=orphan.pk).select_related("parent").get()
    assert reloaded_orphan.parent is None


@pytest.mark.asyncio
async def test_isnull_false_skips_a_row_whose_target_is_soft_deleted(db):
    await create_children()

    assert await names(SoftDeleteGhostChild.objects.filter(parent__isnull=False)) == ["linked"]
    assert await names(SoftDeleteGhostChild.objects.filter(parent__not_isnull=True)) == ["linked"]


@pytest.mark.asyncio
async def test_negated_and_combined_isnull_stay_consistent(db):
    await create_children()

    assert await names(SoftDeleteGhostChild.objects.exclude(parent__isnull=True)) == ["linked"]
    assert await names(SoftDeleteGhostChild.objects.exclude(parent__isnull=False)) == ["orphan", "unlinked"]
    assert await names(SoftDeleteGhostChild.objects.filter(Q(parent__isnull=True) | Q(name="linked"))) == [
        "linked",
        "orphan",
        "unlinked",
    ]
    assert await names(SoftDeleteGhostChild.objects.filter(~Q(parent__isnull=False))) == ["orphan", "unlinked"]


@pytest.mark.asyncio
async def test_key_column_lookup_still_compares_the_stored_value(db):
    await create_children()

    assert await names(SoftDeleteGhostChild.objects.filter(parent_id__isnull=True)) == ["unlinked"]
    assert await names(SoftDeleteGhostChild.objects.filter(parent_id__isnull=False)) == ["linked", "orphan"]


@pytest.mark.asyncio
async def test_count_update_and_delete_use_the_same_isnull_semantics(db):
    await create_children()

    assert await SoftDeleteGhostChild.objects.filter(parent__isnull=True).count() == 2
    assert await SoftDeleteGhostChild.objects.filter(parent__isnull=True).update(name="detached") == 2
    assert await names(SoftDeleteGhostChild.objects.all()) == ["detached", "detached", "linked"]
    assert await SoftDeleteGhostChild.objects.filter(parent__isnull=True).delete() == 2
    assert await names(SoftDeleteGhostChild.objects.all()) == ["linked"]


@pytest.mark.asyncio
async def test_restored_target_counts_as_present_again(db):
    orphan, _, _ = await create_children()
    deleted_parent = await SoftDeleteVersioned.objects.include_deleted().get(name="deleted")

    await deleted_parent.restore()

    assert await names(SoftDeleteGhostChild.objects.filter(parent__isnull=False)) == ["linked", "orphan"]
    assert (await SoftDeleteGhostChild.objects.get(pk=orphan.pk).select_related("parent")).parent.name == "deleted"


@pytest.mark.asyncio
async def test_include_deleted_sees_the_soft_deleted_target_too(db):
    parent = await SoftDeleteParent.objects.create(name="parent")
    await SoftDeleteChildCascadeSoft.objects.create(name="child", parent=parent)
    await parent.delete()

    assert await SoftDeleteChildCascadeSoft.objects.all().count() == 0
    assert await names(SoftDeleteChildCascadeSoft.objects.include_deleted().filter(parent__isnull=True)) == []
    assert await names(SoftDeleteChildCascadeSoft.objects.include_deleted().filter(parent__isnull=False)) == ["child"]
    assert await names(SoftDeleteChildCascadeSoft.objects.only_deleted().filter(parent__isnull=False)) == ["child"]
