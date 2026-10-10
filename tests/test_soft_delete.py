import asyncio
import os
import sys
import types

import pytest
import pytest_asyncio

from hare import Connections, fields
from hare.contrib import test as hare_test
from hare.contrib.test import requires_features
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.exceptions import (
    ConfigurationError,
    DoesNotExist,
    IncompleteInstanceError,
    IntegrityError,
    ProtectedError,
    QueryError,
    StaleObjectError,
)
from hare.models import Model
from hare.models.class_building.model_definition_checks import ModelDefinitionChecks
from hare.time.timezone import Timezone
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    SoftDeleteAutoNow,
    SoftDeleteChildCascadeHard,
    SoftDeleteChildCascadeSoft,
    SoftDeleteChildCascadeSoftWithDeleteOverride,
    SoftDeleteChildProtect,
    SoftDeleteChildProtectO2O,
    SoftDeleteChildRestrict,
    SoftDeleteChildSetDefaultCallable,
    SoftDeleteChildSetNull,
    SoftDeleteComposite,
    SoftDeleteDirtyTracked,
    SoftDeleteGhostChild,
    SoftDeleteM2MParent,
    SoftDeleteM2MPeer,
    SoftDeleteM2MProtectedParent,
    SoftDeleteM2MRestrictedParent,
    SoftDeleteM2MSetNullParent,
    SoftDeleteParent,
    SoftDeleteStandalone,
    SoftDeleteVersioned,
    SoftDeleteVersionedDirtyTracked,
)
from tests.utils.database_under_test import DatabaseUnderTest

# ============================================================================
# Basic delete()/restore(), default-manager auto-filter, include_deleted()
# ============================================================================


@pytest.mark.asyncio
async def test_delete_is_an_update_not_a_delete(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    await parent.delete()

    assert not await SoftDeleteParent.objects.filter(pk=parent.pk).exists()
    assert await SoftDeleteParent.objects.include_deleted().filter(pk=parent.pk).exists()

    restored = await SoftDeleteParent.objects.include_deleted().get(pk=parent.pk)
    assert restored.deleted_at is not None


@pytest.mark.asyncio
async def test_default_manager_hides_deleted_rows(db):
    live = await SoftDeleteParent.objects.create(name="Live")
    deleted = await SoftDeleteParent.objects.create(name="Deleted")
    await deleted.delete()

    names = {p.name for p in await SoftDeleteParent.objects.all()}
    assert names == {"Live"}
    assert await SoftDeleteParent.objects.filter(pk=live.pk).exists()
    assert not await SoftDeleteParent.objects.filter(pk=deleted.pk).exists()


@pytest.mark.asyncio
async def test_refresh_from_db_raises_does_not_exist_for_a_soft_deleted_row(db):
    """A stale in-memory reference to a now-soft-deleted row must refresh through the same
    Meta.soft_delete_field scoping .all()/.filter()/... already apply, not a bare unfiltered
    QuerySet that would silently read the deleted row back."""
    parent = await SoftDeleteParent.objects.create(name="P")
    stale_reference = await SoftDeleteParent.objects.get(pk=parent.pk)
    await parent.delete()

    with pytest.raises(DoesNotExist):
        await stale_reference.refresh_from_db()


@pytest.mark.asyncio
async def test_include_deleted_sees_everything(db):
    await SoftDeleteParent.objects.create(name="Live")
    deleted = await SoftDeleteParent.objects.create(name="Deleted")
    await deleted.delete()

    names = {p.name for p in await SoftDeleteParent.objects.include_deleted().all()}
    assert names == {"Live", "Deleted"}


@pytest.mark.asyncio
async def test_include_deleted_requires_soft_delete_configured(db):
    with pytest.raises(QueryError):
        SoftDeleteChildCascadeHard.objects.include_deleted()


@pytest.mark.asyncio
async def test_restore_reverses_delete(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    await parent.delete()
    await parent.restore()

    assert parent.deleted_at is None
    assert await SoftDeleteParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_restore_requires_soft_delete_configured(db):
    child = await SoftDeleteChildCascadeHard.objects.create(
        name="C", parent=await SoftDeleteParent.objects.create(name="P")
    )
    with pytest.raises(QueryError):
        await child.restore()


@pytest.mark.asyncio
async def test_restore_unpersisted_raises(db):
    parent = SoftDeleteParent(name="Not saved")
    with pytest.raises(Exception):  # noqa: B017 - OperationalError, just needs .pk unset
        await parent.restore()


@pytest.mark.asyncio
async def test_delete_on_only_partial_missing_pk_raises(db):
    """Without this guard, instance.pk silently falls back to None for a partial instance missing
    its own pk column - the soft-delete UPDATE's WHERE clause then matches zero rows with no
    exception, leaving deleted_at set in memory but untouched in the database. Confirmed
    empirically before fixing."""
    created = await SoftDeleteStandalone.objects.create(name="S")
    partial = await SoftDeleteStandalone.objects.get(pk=created.pk).only("name")

    with pytest.raises(IncompleteInstanceError):
        await partial.delete()

    refreshed = await SoftDeleteStandalone.objects.include_deleted().get(pk=created.pk)
    assert refreshed.deleted_at is None


@pytest.mark.asyncio
async def test_restore_on_only_partial_missing_pk_raises(db):
    created = await SoftDeleteStandalone.objects.create(name="S")
    await created.delete()
    partial = await SoftDeleteStandalone.objects.include_deleted().get(pk=created.pk).only("name")

    with pytest.raises(IncompleteInstanceError):
        await partial.restore()

    refreshed = await SoftDeleteStandalone.objects.include_deleted().get(pk=created.pk)
    assert refreshed.deleted_at is not None


# ============================================================================
# Guard against direct field mutation - bypassing the cascade is the whole bug
# ============================================================================


@pytest.mark.asyncio
async def test_direct_assignment_after_persist_raises(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    with pytest.raises(QueryError):
        parent.deleted_at = None  # already None, but still forbidden to touch directly


@pytest.mark.asyncio
async def test_direct_assignment_before_persist_is_allowed(db):
    parent = SoftDeleteParent(name="P")
    parent.deleted_at = None  # not persisted yet - constructing a fresh instance, must not raise
    await parent.save()
    assert (await SoftDeleteParent.objects.get(id=parent.id)).deleted_at is None


@pytest.mark.asyncio
async def test_bulk_update_rejects_soft_delete_field(db):
    await SoftDeleteParent.objects.create(name="P")
    with pytest.raises(QueryError):
        await SoftDeleteParent.objects.all().update(deleted_at=None)


@pytest.mark.asyncio
async def test_bulk_update_of_other_fields_still_works(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    await SoftDeleteParent.objects.filter(pk=parent.pk).update(name="P2")
    assert (await SoftDeleteParent.objects.get(pk=parent.pk)).name == "P2"


# ============================================================================
# Cascade semantics on delete()
# ============================================================================


@pytest.mark.asyncio
async def test_cascade_onto_soft_deletable_child_soft_deletes_it(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="C", parent=parent)

    await parent.delete()

    assert not await SoftDeleteChildCascadeSoft.objects.filter(pk=child.pk).exists()
    restored_child = await SoftDeleteChildCascadeSoft.objects.include_deleted().get(pk=child.pk)
    assert restored_child.deleted_at is not None
    # the physical row must still exist (soft-deleted, not gone)
    assert await SoftDeleteChildCascadeSoft.objects.include_deleted().filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_cascade_calls_a_child_model_own_delete_override(db):
    """The cascade's iterative fast path persists a matching-dispatch-mode child directly via
    _persist_soft_delete()/_persist_hard_delete(), bypassing delete() entirely - which used to
    also silently skip a child model's own delete() override, even though the same override DID
    fire for a dispatch-mode-mismatched child (falls back to a real, recursing delete() call)."""
    SoftDeleteChildCascadeSoftWithDeleteOverride.delete_override_calls.clear()
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeSoftWithDeleteOverride.objects.create(name="C", parent=parent)

    await parent.delete()

    assert SoftDeleteChildCascadeSoftWithDeleteOverride.delete_override_calls == [child.pk]
    restored_child = await SoftDeleteChildCascadeSoftWithDeleteOverride.objects.include_deleted().get(pk=child.pk)
    assert restored_child.deleted_at is not None


@pytest.mark.asyncio
async def test_cascade_does_not_restamp_an_already_soft_deleted_child(db):
    """_related_query() deliberately includes already soft-deleted rows too (PROTECT/RESTRICT/
    further CASCADE hops need to see them), so a child soft-deleted independently, BEFORE its
    parent's own later delete() cascades through it again, used to get its deleted_at silently
    overwritten with a fresh timestamp - losing the real moment it was actually deleted, for no
    reason (the row's own state doesn't change at all)."""
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="C", parent=parent)

    await child.delete()
    original_deleted_at = (await SoftDeleteChildCascadeSoft.objects.include_deleted().get(pk=child.pk)).deleted_at

    await parent.delete()

    after_parent_delete = (await SoftDeleteChildCascadeSoft.objects.include_deleted().get(pk=child.pk)).deleted_at
    assert after_parent_delete == original_deleted_at

    parent_fresh = await SoftDeleteParent.objects.include_deleted().get(pk=parent.pk)
    assert parent_fresh.deleted_at is not None


@pytest.mark.asyncio
async def test_cascade_onto_hard_delete_only_child_keeps_it(db):
    """A soft delete is undone by restore() - a child that can't be soft-deleted itself is kept,
    pointing at the hidden parent, and sees it again once it is restored."""
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeHard.objects.create(name="C", parent=parent)

    await parent.delete()

    fetched = await SoftDeleteChildCascadeHard.objects.get(pk=child.pk).select_related("parent")
    assert fetched.parent is None

    await parent.restore()

    fetched = await SoftDeleteChildCascadeHard.objects.get(pk=child.pk).select_related("parent")
    assert fetched.parent.pk == parent.pk


@pytest.mark.asyncio
async def test_hard_delete_cascades_onto_hard_delete_only_child(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeHard.objects.create(name="C", parent=parent)

    await parent.hard_delete()

    assert not await SoftDeleteChildCascadeHard.objects.filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_protect_blocks_soft_delete_same_as_hard_delete(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildProtect.objects.create(name="C", parent=parent)

    with pytest.raises(ProtectedError):
        await parent.delete()

    # nothing was touched
    assert await SoftDeleteParent.objects.filter(pk=parent.pk).exists()
    parent_fresh = await SoftDeleteParent.objects.get(pk=parent.pk)
    assert parent_fresh.deleted_at is None
    assert await SoftDeleteChildProtect.objects.filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_set_null_nulls_the_fk_on_soft_delete(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildSetNull.objects.create(name="C", parent=parent)

    await parent.delete()

    refreshed = await SoftDeleteChildSetNull.objects.get(pk=child.pk)
    assert refreshed.parent_id is None


@pytest.mark.asyncio
async def test_protect_blocks_soft_delete_through_o2o_relation(db):
    """on_delete=PROTECT on a OneToOneField (not just ForwardKeyField) must block a soft delete
    too - backward_one_to_one_fields was previously invisible to the cascade machinery entirely, so a
    PROTECT-guarded O2O child never stopped the parent from soft-deleting."""
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildProtectO2O.objects.create(name="C", parent=parent)

    with pytest.raises(ProtectedError):
        await parent.delete()

    parent_fresh = await SoftDeleteParent.objects.get(pk=parent.pk)
    assert parent_fresh.deleted_at is None
    assert await SoftDeleteChildProtectO2O.objects.filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_set_default_invokes_callable_default_on_soft_delete(db):
    """SET_DEFAULT's default= may be a callable - it must be invoked, not passed straight into
    the column's to_db_value() as a raw function object."""
    # id=999 must exist for the FK constraint - matches _default_category_id()'s return value.
    default_parent = await SoftDeleteParent.objects.create(id=999, name="Default")
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildSetDefaultCallable.objects.create(name="C", parent=parent)

    await parent.delete()

    refreshed = await SoftDeleteChildSetDefaultCallable.objects.get(pk=child.pk)
    assert refreshed.parent_id == default_parent.pk == 999


@pytest.mark.asyncio
async def test_restrict_blocks_soft_delete(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    await SoftDeleteChildRestrict.objects.create(name="C", parent=parent)

    with pytest.raises(IntegrityError):
        await parent.delete()

    assert await SoftDeleteParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_soft_delete_cascade_is_atomic_when_restrict_blocks_it(db, monkeypatch):
    """apply_soft_delete_cascade must check every RESTRICT/NO_ACTION relation before mutating
    anything - regardless of the arbitrary set-iteration order get_backward_relations() returns
    them in. This forces the exact bad ordering a real run's set-iteration could produce (the
    CASCADE relation visited before the blocking RESTRICT one) and asserts the CASCADE child is
    left untouched once IntegrityError is raised - proving both the up-front RESTRICT check and
    that the whole cascade rolls back as one transaction rather than leaving a partial mutation
    behind."""

    from hare.fields.enums import OnDelete
    from hare.models.deletion.cascade.deletion_graph import DeletionGraph

    original_get_backward_relations = DeletionGraph.get_backward_relations

    def cascade_before_restrict(model):
        relations = original_get_backward_relations(model)
        return sorted(relations, key=lambda pair: pair[1].on_delete == OnDelete.NO_ACTION)

    monkeypatch.setattr(DeletionGraph, "get_backward_relations", cascade_before_restrict)

    parent = await SoftDeleteParent.objects.create(name="P")
    cascade_child = await SoftDeleteChildCascadeHard.objects.create(name="C", parent=parent)
    await SoftDeleteChildRestrict.objects.create(name="R", parent=parent)

    with pytest.raises(IntegrityError):
        await parent.delete()

    assert await SoftDeleteParent.objects.filter(pk=parent.pk).exists()
    parent_fresh = await SoftDeleteParent.objects.get(pk=parent.pk)
    assert parent_fresh.deleted_at is None
    # The CASCADE relation was visited (and would have hard-deleted this child) before the
    # RESTRICT check under the old ordering - it must not have been touched.
    assert await SoftDeleteChildCascadeHard.objects.filter(pk=cascade_child.pk).exists()


@pytest.mark.asyncio
async def test_bulk_soft_delete_is_atomic_when_a_later_row_is_restricted(db_truncate):
    """QuerySet.delete() on a soft-delete model with any backward relation soft-deletes each
    matching row through its own Model.delete() - each opening its own transaction, so a RESTRICT
    failure on a later row used to leave every earlier row already soft-deleted, unlike the single
    statement a model without incoming relations gets (all rows or none)."""
    first_parent = await SoftDeleteParent.objects.create(name="first")
    second_parent = await SoftDeleteParent.objects.create(name="second")
    restricted_parent = await SoftDeleteParent.objects.create(name="restricted")
    await SoftDeleteChildRestrict.objects.create(name="blocker", parent=restricted_parent)

    with pytest.raises(IntegrityError):
        await SoftDeleteParent.objects.all().delete()

    live_parent_names = {parent.name async for parent in SoftDeleteParent.objects.all()}
    assert live_parent_names == {first_parent.name, second_parent.name, restricted_parent.name}
    assert all(parent.deleted_at is None for parent in await SoftDeleteParent.objects.include_deleted().all())


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_soft_delete_failure_rolls_back_only_to_its_own_savepoint_inside_an_outer_transaction(db_truncate):
    """Inside a caller's transaction the shared wrapper is a savepoint - a failed bulk soft-delete
    undoes just its own partial work and leaves the outer transaction usable, with the caller's
    earlier writes intact."""
    first_parent = await SoftDeleteParent.objects.create(name="first")
    restricted_parent = await SoftDeleteParent.objects.create(name="restricted")
    await SoftDeleteChildRestrict.objects.create(name="blocker", parent=restricted_parent)

    async with Transactions.atomic():
        marker = await SoftDeleteStandalone.objects.create(name="marker")
        with pytest.raises(IntegrityError):
            await SoftDeleteParent.objects.all().delete()
        await SoftDeleteStandalone.objects.filter(pk=marker.pk).update(name="marker-updated")

    assert (await SoftDeleteStandalone.objects.get(pk=marker.pk)).name == "marker-updated"
    assert (await SoftDeleteParent.objects.get(pk=first_parent.pk)).deleted_at is None
    assert await SoftDeleteParent.objects.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_soft_delete_of_several_rows_still_succeeds_and_cascades(db_truncate):
    """The shared transaction doesn't change the success path - every matching row is
    soft-deleted, its cascade applied, and the count returned."""
    first_parent = await SoftDeleteParent.objects.create(name="first")
    second_parent = await SoftDeleteParent.objects.create(name="second")
    first_child = await SoftDeleteChildCascadeSoft.objects.create(name="c1", parent=first_parent)
    second_child = await SoftDeleteChildCascadeSoft.objects.create(name="c2", parent=second_parent)

    deleted_count = await SoftDeleteParent.objects.all().delete()

    assert deleted_count == 2
    assert await SoftDeleteParent.objects.all().count() == 0
    assert await SoftDeleteChildCascadeSoft.objects.all().count() == 0
    assert {child.pk for child in await SoftDeleteChildCascadeSoft.objects.include_deleted().all()} == {
        first_child.pk,
        second_child.pk,
    }


@pytest.mark.asyncio
async def test_delete_with_no_related_rows_succeeds(db):
    parent = await SoftDeleteParent.objects.create(name="Lonely")
    await parent.delete()
    assert not await SoftDeleteParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_stale_full_save_does_not_resurrect_a_soft_deleted_row(db):
    """A full save() from an instance whose in-memory soft_delete_field is still None (read
    before another instance of the same row was deleted) must not write deleted_at=NULL - only
    delete()/restore() are allowed to change that field."""
    original = await SoftDeleteStandalone.objects.create(name="a")
    other_instance = await SoftDeleteStandalone.objects.get(pk=original.pk)
    await other_instance.delete()

    original.name = "changed"
    await original.save()

    assert not await SoftDeleteStandalone.objects.filter(pk=original.pk).exists()
    still_deleted = await SoftDeleteStandalone.objects.include_deleted().get(pk=original.pk)
    assert still_deleted.deleted_at is not None
    assert still_deleted.name == "changed"


@pytest.mark.asyncio
async def test_repeated_delete_on_already_soft_deleted_instance_is_a_noop(db):
    """A second delete() on an already soft-deleted instance must not overwrite deleted_at with
    a fresh timestamp, same as a second remove()/clear() on an already soft-deleted M2M
    through row is already a no-op."""
    instance = await SoftDeleteStandalone.objects.create(name="e")
    await instance.delete()
    first_deleted_at = (await SoftDeleteStandalone.objects.include_deleted().get(pk=instance.pk)).deleted_at

    await instance.delete()

    second_deleted_at = (await SoftDeleteStandalone.objects.include_deleted().get(pk=instance.pk)).deleted_at
    assert second_deleted_at == first_deleted_at


@pytest.mark.asyncio
async def test_restore_does_not_cascade_to_children(db):
    """Deliberate asymmetry - see Model.restore()'s docstring for why."""
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="C", parent=parent)
    await parent.delete()

    await parent.restore()

    assert await SoftDeleteParent.objects.filter(pk=parent.pk).exists()
    assert not await SoftDeleteChildCascadeSoft.objects.filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_soft_delete_keeps_m2m_links_on_forward_declared_field(db):
    """A soft-deleted row's M2M links are kept: hidden while the row is deleted, back with
    restore(). The peer itself is untouched."""
    parent = await SoftDeleteM2MParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.peers.add(peer)

    await parent.delete()

    assert await SoftDeleteM2MParent.objects.include_deleted().filter(pk=parent.pk, peers=peer).exists()
    assert not await peer.parents.all().exists()
    peer_fresh = await SoftDeleteM2MPeer.objects.get(pk=peer.pk)
    assert peer_fresh.deleted_at is None

    await parent.restore()

    assert [linked.pk for linked in await peer.parents.all()] == [parent.pk]


@pytest.mark.asyncio
async def test_soft_delete_keeps_m2m_links_on_generated_backward_field(db):
    """Same as above, but soft-deleting from the OTHER side of the relation - through the
    auto-generated backward m2m field rather than the one declared directly on the model."""
    parent = await SoftDeleteM2MParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.peers.add(peer)

    await peer.delete()

    assert await SoftDeleteM2MPeer.objects.include_deleted().filter(pk=peer.pk, parents=parent).exists()
    assert not await parent.peers.all().exists()

    await peer.restore()

    assert [linked.pk for linked in await parent.peers.all()] == [peer.pk]


@pytest.mark.asyncio
async def test_hard_delete_removes_m2m_links(db):
    parent = await SoftDeleteM2MParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.peers.add(peer)

    await parent.hard_delete()

    assert not await SoftDeleteM2MPeer.objects.include_deleted().filter(pk=peer.pk, parents__isnull=False).exists()


@pytest.mark.asyncio
async def test_m2m_restrict_blocks_soft_delete(db):
    parent = await SoftDeleteM2MRestrictedParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.restricted_peers.add(peer)

    with pytest.raises(IntegrityError):
        await parent.delete()

    assert await SoftDeleteM2MRestrictedParent.objects.filter(pk=parent.pk).exists()
    parent_fresh = await SoftDeleteM2MRestrictedParent.objects.get(pk=parent.pk)
    assert parent_fresh.deleted_at is None
    # the link itself must be untouched too, not partially cleared before the block was found
    assert await SoftDeleteM2MRestrictedParent.objects.filter(pk=parent.pk, restricted_peers=peer).exists()


@pytest.mark.asyncio
async def test_m2m_restrict_blocks_soft_delete_from_generated_backward_field(db):
    """Same as test_m2m_restrict_blocks_soft_delete, but soft-deleting from the OTHER side of
    the relation - Apps.init_relations() used to construct the auto-generated backward
    ManyToManyFieldInstance without forwarding the forward field's on_delete, so it always
    defaulted to CASCADE regardless of what was actually declared. Soft-deleting a peer through
    the generated backward accessor silently bypassed the RESTRICT/NO_ACTION protection that
    correctly blocks deleting from the forward (parent) side."""
    parent = await SoftDeleteM2MRestrictedParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.restricted_peers.add(peer)

    with pytest.raises(IntegrityError):
        await peer.delete()

    assert await SoftDeleteM2MPeer.objects.filter(pk=peer.pk).exists()
    peer_fresh = await SoftDeleteM2MPeer.objects.get(pk=peer.pk)
    assert peer_fresh.deleted_at is None
    assert await SoftDeleteM2MRestrictedParent.objects.filter(pk=parent.pk, restricted_peers=peer).exists()


@pytest.mark.asyncio
async def test_m2m_protect_blocks_soft_delete(db):
    parent = await SoftDeleteM2MProtectedParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.protected_peers.add(peer)

    with pytest.raises(ProtectedError):
        await parent.delete()

    assert await SoftDeleteM2MProtectedParent.objects.filter(pk=parent.pk).exists()
    parent_fresh = await SoftDeleteM2MProtectedParent.objects.get(pk=parent.pk)
    assert parent_fresh.deleted_at is None
    # the link itself must be untouched too, not partially cleared before the block was found
    assert await SoftDeleteM2MProtectedParent.objects.filter(pk=parent.pk, protected_peers=peer).exists()


@pytest.mark.asyncio
async def test_m2m_protect_blocks_soft_delete_from_generated_backward_field(db):
    """Same as test_m2m_protect_blocks_soft_delete, but soft-deleting from the OTHER side of the
    relation - on_delete is mirrored onto the auto-generated backward field (see
    Apps.init_relations), so PROTECT has to block from either side."""
    parent = await SoftDeleteM2MProtectedParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.protected_peers.add(peer)

    with pytest.raises(ProtectedError):
        await peer.delete()

    assert await SoftDeleteM2MPeer.objects.filter(pk=peer.pk).exists()
    peer_fresh = await SoftDeleteM2MPeer.objects.get(pk=peer.pk)
    assert peer_fresh.deleted_at is None


@pytest.mark.asyncio
async def test_m2m_set_null_soft_delete_keeps_through_row(db):
    """A soft delete leaves an on_delete=SET_NULL relation's through row as it is - both keys
    kept, so restore() brings the link back - checked directly against the through table."""
    parent = await SoftDeleteM2MSetNullParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.nullable_peers.add(peer)

    field = SoftDeleteM2MSetNullParent._meta.fields_map["nullable_peers"]
    through_table = field.through
    backward_column = field.backward_keys[0]
    forward_column = field.forward_keys[0]

    await parent.delete()

    conn = Connections.get("models")
    rows = await conn.execute_dicts(f'SELECT * FROM "{through_table}"')
    assert len(rows) == 1
    assert rows[0][backward_column] == parent.pk
    assert rows[0][forward_column] == peer.pk

    await parent.restore()

    assert [linked.pk for linked in await parent.nullable_peers.all()] == [peer.pk]


@pytest.mark.asyncio
async def test_m2m_set_null_hard_delete_nulls_out_through_row(db):
    parent = await SoftDeleteM2MSetNullParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.nullable_peers.add(peer)

    field = SoftDeleteM2MSetNullParent._meta.fields_map["nullable_peers"]
    through_table = field.through
    backward_column = field.backward_keys[0]
    forward_column = field.forward_keys[0]

    await parent.hard_delete()

    conn = Connections.get("models")
    rows = await conn.execute_dicts(f'SELECT * FROM "{through_table}"')
    assert len(rows) == 1
    assert rows[0][backward_column] is None
    assert rows[0][forward_column] == peer.pk


# ============================================================================
# ManyToManyRelation.add() must refuse to link a soft-deleted instance
# ============================================================================


@pytest.mark.asyncio
async def test_m2m_add_rejects_soft_deleted_owning_instance(db):
    """The default manager's ambient scope already hides a soft-deleted row from .filter()/
    .update() - add() instead operates on a Model instance the caller already holds a reference
    to, bypassing that ambient scope entirely, so it needs its own explicit check."""
    parent = await SoftDeleteM2MParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.delete()

    with pytest.raises(IntegrityError):
        await parent.peers.add(peer)

    assert not await SoftDeleteM2MParent.objects.include_deleted().filter(pk=parent.pk, peers=peer).exists()


@pytest.mark.asyncio
async def test_m2m_add_rejects_soft_deleted_related_instance(db):
    parent = await SoftDeleteM2MParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await peer.delete()

    with pytest.raises(IntegrityError):
        await parent.peers.add(peer)

    assert not await SoftDeleteM2MParent.objects.filter(pk=parent.pk, peers=peer).exists()


@pytest.mark.asyncio
async def test_m2m_add_allows_live_instances_after_unrelated_soft_delete(db):
    """The new guard must not become overzealous - adding two still-live instances must keep
    working even though the model class involved has Meta.soft_delete_field configured."""
    parent = await SoftDeleteM2MParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")

    await parent.peers.add(peer)

    assert await SoftDeleteM2MParent.objects.filter(pk=parent.pk, peers=peer).exists()


# ============================================================================
# Bulk QuerySet.delete()
# ============================================================================


@pytest.mark.asyncio
async def test_bulk_delete_fast_path_no_relations(db):
    a = await SoftDeleteStandalone.objects.create(name="A")
    b = await SoftDeleteStandalone.objects.create(name="B")

    result = await SoftDeleteStandalone.objects.all().delete()

    assert result == 2
    assert not await SoftDeleteStandalone.objects.filter(pk=a.pk).exists()
    assert not await SoftDeleteStandalone.objects.filter(pk=b.pk).exists()
    assert await SoftDeleteStandalone.objects.include_deleted().filter(pk=a.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_with_relations_cascades_per_row(db):
    parent_1 = await SoftDeleteParent.objects.create(name="P1")
    parent_2 = await SoftDeleteParent.objects.create(name="P2")
    child_1 = await SoftDeleteChildCascadeSoft.objects.create(name="C1", parent=parent_1)
    child_2 = await SoftDeleteChildCascadeSoft.objects.create(name="C2", parent=parent_2)

    result = await SoftDeleteParent.objects.all().delete()

    assert result == 2
    assert not await SoftDeleteParent.objects.filter(pk__in=[parent_1.pk, parent_2.pk]).exists()
    assert not await SoftDeleteChildCascadeSoft.objects.filter(pk__in=[child_1.pk, child_2.pk]).exists()
    assert await SoftDeleteChildCascadeSoft.objects.include_deleted().filter(pk=child_1.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_protected_rejects_whole_batch(db):
    protected_parent = await SoftDeleteParent.objects.create(name="Protected")
    await SoftDeleteChildProtect.objects.create(name="C", parent=protected_parent)
    free_parent = await SoftDeleteParent.objects.create(name="Free")

    with pytest.raises(ProtectedError):
        await SoftDeleteParent.objects.all().delete()

    assert await SoftDeleteParent.objects.filter(pk=protected_parent.pk).exists()
    assert await SoftDeleteParent.objects.filter(pk=free_parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_no_matches_returns_zero(db):
    result = await SoftDeleteParent.objects.filter(name="does not exist").delete()
    assert result == 0


@pytest.mark.asyncio
async def test_bulk_delete_filtered_subset_only_affects_matches(db):
    keep = await SoftDeleteStandalone.objects.create(name="Keep")
    remove = await SoftDeleteStandalone.objects.create(name="Remove")

    result = await SoftDeleteStandalone.objects.filter(name="Remove").delete()

    assert result == 1
    assert await SoftDeleteStandalone.objects.filter(pk=keep.pk).exists()
    assert not await SoftDeleteStandalone.objects.filter(pk=remove.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_m2m_only_relation_keeps_links(db):
    """The bulk-delete path keeps a soft-deleted row's M2M links like Model.delete(): hidden while
    the row is deleted, back with restore(). The peer itself is untouched."""
    parent = await SoftDeleteM2MParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.peers.add(peer)

    result = await SoftDeleteM2MParent.objects.filter(pk=parent.pk).delete()

    assert result == 1
    assert await SoftDeleteM2MParent.objects.include_deleted().filter(pk=parent.pk, peers=peer).exists()
    assert not await peer.parents.all().exists()
    assert await SoftDeleteM2MPeer.objects.filter(pk=peer.pk).exists()

    await parent.restore()

    assert [linked.pk for linked in await peer.parents.all()] == [parent.pk]


@pytest.mark.asyncio
async def test_bulk_delete_m2m_only_relation_keeps_set_null_through_row(db):
    """Sibling of test_m2m_set_null_soft_delete_keeps_through_row for the bulk-delete path."""
    parent = await SoftDeleteM2MSetNullParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.nullable_peers.add(peer)

    field = SoftDeleteM2MSetNullParent._meta.fields_map["nullable_peers"]
    through_table = field.through
    backward_column = field.backward_keys[0]
    forward_column = field.forward_keys[0]

    result = await SoftDeleteM2MSetNullParent.objects.filter(pk=parent.pk).delete()

    assert result == 1
    conn = Connections.get("models")
    rows = await conn.execute_dicts(f'SELECT * FROM "{through_table}"')
    assert len(rows) == 1
    assert rows[0][backward_column] == parent.pk
    assert rows[0][forward_column] == peer.pk


@pytest.mark.asyncio
async def test_bulk_delete_m2m_only_relation_still_enforces_protect(db):
    """Sibling of test_m2m_protect_blocks_soft_delete for the bulk-delete path - without also
    checking get_many_to_many_fields() in the fast-path guard, this used to succeed silently (a real
    data-integrity gap: PROTECT never raised at all, and the row was soft-deleted anyway)."""
    parent = await SoftDeleteM2MProtectedParent.objects.create(name="P")
    peer = await SoftDeleteM2MPeer.objects.create(name="Peer")
    await parent.protected_peers.add(peer)

    with pytest.raises(ProtectedError):
        await SoftDeleteM2MProtectedParent.objects.filter(pk=parent.pk).delete()

    assert await SoftDeleteM2MProtectedParent.objects.filter(pk=parent.pk).exists()
    parent_fresh = await SoftDeleteM2MProtectedParent.objects.get(pk=parent.pk)
    assert parent_fresh.deleted_at is None
    assert await SoftDeleteM2MProtectedParent.objects.filter(pk=parent.pk, protected_peers=peer).exists()


# ============================================================================
# Meta.soft_delete_field validation
# ============================================================================


def test_soft_delete_field_must_exist(db):
    """Unit-tests MetaInfo's own validation directly - going through a full Hare.init() app
    registration just to exercise this one validation branch would require a whole separate
    fixture module (see tests/schema/models_fk_2.py's pattern) for a config-time check that
    doesn't depend on that machinery at all."""
    meta = SoftDeleteStandalone._meta
    original = meta.soft_delete_field
    meta.soft_delete_field = "not_a_real_field"
    try:
        with pytest.raises(ConfigurationError):
            ModelDefinitionChecks.validate_soft_delete_field(meta)
    finally:
        meta.soft_delete_field = original


def test_soft_delete_field_must_be_nullable_datetime(db):
    meta = SoftDeleteChildCascadeHard._meta
    original = meta.soft_delete_field
    meta.soft_delete_field = "name"  # a real field, but a TextField, not DatetimeField(null=True)
    try:
        with pytest.raises(ConfigurationError):
            ModelDefinitionChecks.validate_soft_delete_field(meta)
    finally:
        meta.soft_delete_field = original


# ============================================================================
# Further combinations: optimistic_lock_field, track_dirty_fields, composite PK, Transactions.autonomous(),
# values()/values_list()
# ============================================================================


@pytest_asyncio.fixture
async def file_db(tmp_path):
    """A real, persistent connection - Transactions.autonomous() opens a genuinely separate
    connection, and a fresh connection to `:memory:` sqlite is a completely separate, empty
    database. Mirrors the ambient HARE_TEST_DB when it's already a real (Postgres) URL, so a
    Postgres-only test can get a real Postgres "models" connection - falls back to a real
    file-backed SQLite DB otherwise."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    db_url = os.getenv("HARE_TEST_DB")
    if not db_url or DatabaseUnderTest.is_file_database(db_url):
        scheme = (db_url or DatabaseUnderTest.DEFAULT_URL).split("://", 1)[0]
        db_path = tmp_path / "soft_delete_autonomous_test.sqlite"
        db_url = f"{scheme}:///{db_path}?synchronous=OFF"
    async with hare_test_context(["tests.testmodels"], db_url=db_url, connection_label="models") as ctx:
        yield ctx


@pytest.mark.asyncio
async def test_delete_bumps_optimistic_lock_field_on_same_model(db):
    """delete()'s soft-delete UPDATE goes through the same executor path save() uses - it bumps
    optimistic_lock_field unconditionally, same as any other write to the row would. Any modification to
    the row (soft-deleting it included) invalidates a concurrently-held stale version, which is
    the whole point of optimistic locking."""
    obj = await SoftDeleteVersioned.objects.create(name="A")
    assert obj.version == 0

    await obj.delete()

    refreshed = await SoftDeleteVersioned.objects.include_deleted().get(pk=obj.pk)
    assert refreshed.deleted_at is not None
    assert refreshed.version == 1


@pytest.mark.asyncio
async def test_restore_bumps_optimistic_lock_field_on_same_model(db):
    obj = await SoftDeleteVersioned.objects.create(name="A")
    await obj.delete()
    deleted = await SoftDeleteVersioned.objects.include_deleted().get(pk=obj.pk)

    await deleted.restore()

    refreshed = await SoftDeleteVersioned.objects.get(pk=obj.pk)
    assert refreshed.deleted_at is None
    assert refreshed.version == 2


@pytest.mark.asyncio
async def test_bulk_update_writes_soft_deleted_row(db):
    """bulk_update() runs on an object it already holds, matched by its own PK - like save(), not
    like a filtered bulk operation - so it must not inherit the default manager's implicit
    `deleted_at IS NULL` filter. That filter used to get folded into the per-object
    `WHERE pk = ... AND (deleted_at IS NULL)`, so updating a soft-deleted instance by its own PK
    silently matched 0 rows and the write vanished with no exception."""
    obj = await SoftDeleteStandalone.objects.create(name="A")
    await obj.delete()

    obj.name = "A2"
    rows_affected = await SoftDeleteStandalone.objects.bulk_update([obj], fields=["name"])
    assert rows_affected == 1

    refreshed = await SoftDeleteStandalone.objects.include_deleted().get(pk=obj.pk)
    assert refreshed.name == "A2"
    assert refreshed.deleted_at is not None


@pytest.mark.asyncio
async def test_bulk_update_writes_soft_deleted_row_with_optimistic_lock_field(db):
    """Same gap as test_bulk_update_writes_soft_deleted_row, but on a model that also has
    Meta.optimistic_lock_field - the ambient soft-delete filter used to make bulk_update() match 0 of 1
    rows, which (unlike the plain model above) didn't just silently no-op: it raised
    StaleObjectError, misleadingly implying the row had been concurrently modified when it had
    really just been excluded by this filter."""
    obj = await SoftDeleteVersioned.objects.create(name="A")
    await obj.delete()

    deleted = await SoftDeleteVersioned.objects.include_deleted().get(pk=obj.pk)
    version_before_bulk_update = deleted.version
    deleted.name = "A2"
    rows_affected = await SoftDeleteVersioned.objects.bulk_update([deleted], fields=["name"])
    assert rows_affected == 1

    refreshed = await SoftDeleteVersioned.objects.include_deleted().get(pk=obj.pk)
    assert refreshed.name == "A2"
    assert refreshed.deleted_at is not None
    assert refreshed.version == version_before_bulk_update + 1


@pytest.mark.asyncio
async def test_bulk_update_rejects_soft_delete_field_on_forged_unpersisted_object(db):
    """bulk_update()'s contract is "update objects I already hold by PK", not a filtered query -
    unlike .update(), it had no guard against writing Meta.soft_delete_field directly. A caller
    can construct a fresh, never-persisted object carrying an existing row's own PK and set the
    soft-delete field on it directly - Model.__setattr__'s own guard only fires for an
    already-persisted instance, so this assignment is allowed - then push it through
    bulk_update(), bypassing both the field-level guard AND the soft-delete cascade/PROTECT
    checks .delete()/.restore()/.update() all enforce. Confirmed as a live bug: without this
    guard, the row below silently gets soft-deleted."""
    existing = await SoftDeleteStandalone.objects.create(name="P")

    forged = SoftDeleteStandalone(name="P")
    forged.pk = existing.pk
    forged.deleted_at = Timezone.now()

    with pytest.raises(QueryError):
        await SoftDeleteStandalone.objects.bulk_update([forged], fields=["deleted_at"])

    refreshed = await SoftDeleteStandalone.objects.get(pk=existing.pk)
    assert refreshed.deleted_at is None


@pytest.mark.asyncio
async def test_stale_delete_rolls_back_soft_delete_field_not_just_version(db):
    """delete() sets the soft-delete field eagerly, before the write outcome is known, exactly
    like the version bump - a delete() that fails with StaleObjectError (the UPDATE's WHERE
    matched nothing) must not leave this instance claiming to be deleted while the DB row is
    still live. Confirmed as a live bug: only version rolled back here, deleted_at stayed set."""
    obj = await SoftDeleteVersioned.objects.create(name="A")
    copy_1 = await SoftDeleteVersioned.objects.get(pk=obj.pk)
    copy_2 = await SoftDeleteVersioned.objects.get(pk=obj.pk)

    copy_1.name = "A2"
    await copy_1.save()  # bumps version to 1, row stays live

    assert copy_2.deleted_at is None
    with pytest.raises(StaleObjectError):
        await copy_2.delete()

    assert copy_2.deleted_at is None
    assert copy_2.version == 0

    still_live = await SoftDeleteVersioned.objects.get(pk=obj.pk)
    assert still_live.deleted_at is None


@pytest.mark.asyncio
async def test_stale_restore_rolls_back_soft_delete_field_not_just_version(db):
    """Mirror of the delete() case above: restore() clears the soft-delete field eagerly before
    the write outcome is known - a restore() that fails with StaleObjectError must not leave this
    instance falsely reporting itself as live while the DB row is still soft-deleted."""
    obj = await SoftDeleteVersioned.objects.create(name="A")
    await obj.delete()

    copy_1 = await SoftDeleteVersioned.objects.include_deleted().get(pk=obj.pk)
    copy_2 = await SoftDeleteVersioned.objects.include_deleted().get(pk=obj.pk)
    stale_deleted_at = copy_2.deleted_at
    assert stale_deleted_at is not None

    await copy_1.restore()  # bumps version, row is live again

    with pytest.raises(StaleObjectError):
        await copy_2.restore()

    assert copy_2.deleted_at == stale_deleted_at
    assert copy_2.version == 1

    still_restored = await SoftDeleteVersioned.objects.get(pk=obj.pk)
    assert still_restored.deleted_at is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_savepoint_rollback_for_an_unrelated_reason_rolls_back_soft_delete_field_too(db):
    """delete() itself SUCCEEDS here (a real row match, no exception, no stale conflict) - the
    two tests above cover THIS delete()/restore() call failing on its own. But if the enclosing
    transaction/savepoint later rolls back anyway, for a reason entirely unrelated to this
    delete(), the DB row correctly reverts via the real SQL ROLLBACK - deleted_at and version
    both need rolling back too, or this instance is left permanently claiming to be deleted
    (and one version ahead) even though the database never actually persisted either change."""

    class BoomError(Exception):
        pass

    obj = await SoftDeleteVersioned.objects.create(name="A")

    with pytest.raises(BoomError):
        async with Transactions.atomic():
            await obj.delete()
            raise BoomError("unrelated failure, after delete() already succeeded")

    assert obj.deleted_at is None
    assert obj.version == 0

    fresh = await SoftDeleteVersioned.objects.get(pk=obj.pk)
    assert fresh.deleted_at is None
    assert fresh.version == 0

    await obj.delete()  # must not raise StaleObjectError
    await obj.restore()


@pytest.mark.asyncio
async def test_delete_syncs_only_the_soft_delete_field_in_dirty_snapshot(db):
    """delete()/restore() bypass save() entirely, so they need their own targeted dirty-snapshot
    update - a full re-snapshot would incorrectly launder any OTHER unsaved change (name here) as
    clean too. Confirmed empirically before fixing: get_dirty_fields() kept reporting deleted_at
    as dirty even after delete() had already persisted it."""
    obj = await SoftDeleteDirtyTracked.objects.create(name="A")
    obj.name = "A2"
    assert obj.get_dirty_fields() == {"name": ("A", "A2")}

    await obj.delete()

    assert obj.get_dirty_fields() == {"name": ("A", "A2")}


@pytest.mark.asyncio
async def test_restore_syncs_only_the_soft_delete_field_in_dirty_snapshot(db):
    obj = await SoftDeleteDirtyTracked.objects.create(name="A")
    await obj.delete()
    obj.name = "A2"
    assert obj.get_dirty_fields() == {"name": ("A", "A2")}

    await obj.restore()

    assert obj.get_dirty_fields() == {"name": ("A", "A2")}


@pytest.mark.asyncio
async def test_soft_delete_on_composite_pk_model(db):
    obj = await SoftDeleteComposite.objects.create(a=1, b=2, name="X")

    await obj.delete()

    assert not await SoftDeleteComposite.objects.filter(a=1, b=2).exists()
    refreshed = await SoftDeleteComposite.objects.include_deleted().get(a=1, b=2)
    assert refreshed.deleted_at is not None

    await refreshed.restore()
    assert await SoftDeleteComposite.objects.filter(a=1, b=2).exists()


@pytest.mark.asyncio
async def test_soft_delete_inside_autonomous_transaction(file_db):
    """delete()'s UPDATE works the same on an autonomous() connection as it does on the default
    one - it's a plain UPDATE under the hood, nothing about it is transaction-scope-specific."""
    obj = await SoftDeleteStandalone.objects.create(name="A")

    async with Transactions.autonomous() as conn:
        await obj.delete(using=conn)

    refreshed = await SoftDeleteStandalone.objects.include_deleted().get(pk=obj.pk)
    assert refreshed.deleted_at is not None


@hare_test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_soft_delete_cascade_uses_the_same_connection_as_using_db(file_db):
    """The soft-delete UPDATE on the instance itself and the CASCADE-triggered delete on its
    children must go through the SAME connection - otherwise using=/Transactions.autonomous()
    stops being atomic with the cascade it triggers. Reproduced the bug empirically before fixing:
    parent.delete(using=<autonomous connection>) committed independently (as intended), but the
    CASCADE child delete silently fell back to the default connection, tied to the enclosing
    (here, rolled-back) transaction - leaving the child alive even though its parent was already
    marked deleted. Postgres-only: needs two genuinely independent, concurrently-open connections,
    which SQLite's writer serialization makes unreliable to demonstrate (see the equivalent
    reasoning in test_optimistic_locking.py's autonomous-race test)."""
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="C", parent=parent)

    with pytest.raises(IntegrityError):
        async with Transactions.atomic():
            async with Transactions.autonomous() as conn:
                await parent.delete(using=conn)
            raise IntegrityError("force rollback of the OUTER (non-autonomous) transaction")

    parent_after = await SoftDeleteParent.objects.include_deleted().get(pk=parent.pk)
    assert parent_after.deleted_at is not None  # survived the rollback - went through the autonomous conn

    # the CASCADE-triggered soft delete of the child must have gone through that SAME autonomous
    # connection, so it's just as committed and just as unaffected by the outer rollback.
    assert not await SoftDeleteChildCascadeSoft.objects.filter(pk=child.pk).exists()
    assert (await SoftDeleteChildCascadeSoft.objects.include_deleted().get(pk=child.pk)).deleted_at is not None


@pytest.mark.asyncio
async def test_values_and_values_list_respect_default_manager_auto_filter(db):
    alive = await SoftDeleteStandalone.objects.create(name="alive")
    deleted = await SoftDeleteStandalone.objects.create(name="deleted")
    await deleted.delete()

    values_rows = await SoftDeleteStandalone.objects.all().values("name")
    assert values_rows == [{"name": "alive"}]

    values_list_rows = await SoftDeleteStandalone.objects.all().values_list("name", flat=True)
    assert values_list_rows == [alive.name]

    all_values = await SoftDeleteStandalone.objects.include_deleted().values("name")
    assert {row["name"] for row in all_values} == {"alive", "deleted"}


@pytest.mark.asyncio
async def test_select_related_agrees_with_prefetch_related_on_soft_deleted_related_row(db):
    """select_related()'s JOIN now folds in the SAME Meta.soft_delete_field filter the related
    model's own Manager.get_queryset() applies - previously it was a raw JOIN that never went
    through that manager at all, so a live row pointing at an already-soft-deleted related row
    (soft-deleting something doesn't prevent a later insert from pointing a new FK at it) came
    back populated via select_related() while prefetch_related() (a genuinely separate query
    through the related model's normal queryset) silently returned None for the exact same row.
    The two must agree now."""
    parent = await SoftDeleteVersioned.objects.create(name="P")
    await parent.delete()
    child = await SoftDeleteGhostChild.objects.create(name="C", parent=parent)

    via_select_related = await SoftDeleteGhostChild.objects.filter(pk=child.pk).select_related("parent").first()
    assert via_select_related.parent is None

    via_prefetch_related = await SoftDeleteGhostChild.objects.filter(pk=child.pk).prefetch_related("parent").first()
    assert via_prefetch_related.parent is None


@pytest.mark.asyncio
async def test_delete_and_restore_on_pk_only_instance_of_model_with_auto_now_field(db):
    """delete()/restore() only WRITE an auto_now field, so an .only("id") instance that never
    loaded it must work - the executor's old-value snapshot used to crash with AttributeError."""
    created = await SoftDeleteAutoNow.objects.create(name="S")
    partial = await SoftDeleteAutoNow.objects.filter(id=created.id).only("id").get()
    assert not hasattr(partial, "updated_at")

    await partial.delete()

    deleted = await SoftDeleteAutoNow.objects.include_deleted().get(id=created.id)
    assert deleted.deleted_at is not None
    assert deleted.updated_at > created.updated_at

    partial_deleted = await SoftDeleteAutoNow.objects.include_deleted().filter(id=created.id).only("id").get()
    await partial_deleted.restore()

    restored = await SoftDeleteAutoNow.objects.get(id=created.id)
    assert restored.deleted_at is None
    assert restored.updated_at > deleted.updated_at


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_rolled_back_delete_on_pk_only_instance_unloads_auto_now_field_again(db):
    created = await SoftDeleteAutoNow.objects.create(name="S")
    partial = await SoftDeleteAutoNow.objects.filter(id=created.id).only("id").get()

    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            await partial.delete()
            assert hasattr(partial, "updated_at")
            raise RuntimeError("roll back")

    assert not hasattr(partial, "updated_at")
    assert not hasattr(partial, "deleted_at")
    assert await SoftDeleteAutoNow.objects.filter(id=created.id).exists()


@pytest.mark.asyncio
async def test_delete_and_restore_with_unloaded_optimistic_lock_field_raise_incomplete_instance_error(db):
    """A soft delete bumps Meta.optimistic_lock_field like any other write, so a partial instance that
    never loaded it needs the same clear error save() gives, not a raw AttributeError."""
    created = await SoftDeleteVersioned.objects.create(name="S")
    partial = await SoftDeleteVersioned.objects.filter(id=created.id).only("id").get()

    with pytest.raises(IncompleteInstanceError, match="optimistic_lock_field"):
        await partial.delete()

    with_version = await SoftDeleteVersioned.objects.filter(id=created.id).only("id", "version").get()
    await with_version.delete()
    assert with_version.version == 1

    partial_deleted = await SoftDeleteVersioned.objects.include_deleted().filter(id=created.id).only("id").get()
    with pytest.raises(IncompleteInstanceError, match="optimistic_lock_field"):
        await partial_deleted.restore()


@pytest.mark.asyncio
async def test_delete_of_a_partially_loaded_already_soft_deleted_instance_is_a_no_op(db):
    parent = await SoftDeleteParent.objects.create(name="p")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="c", parent=parent)
    await parent.delete()
    original_deleted_at = (await SoftDeleteParent.objects.include_deleted().get(pk=parent.pk)).deleted_at
    await child.restore()

    partial_parent = await SoftDeleteParent.objects.all().only_deleted().only("id", "name").get(pk=parent.pk)
    await partial_parent.delete()

    assert (await SoftDeleteParent.objects.include_deleted().get(pk=parent.pk)).deleted_at == original_deleted_at
    assert (await SoftDeleteChildCascadeSoft.objects.get(pk=child.pk)).deleted_at is None
    assert not hasattr(partial_parent, "deleted_at")


@pytest.mark.asyncio
async def test_delete_of_a_partially_loaded_live_instance_still_cascades(db):
    parent = await SoftDeleteParent.objects.create(name="p")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="c", parent=parent)

    partial_parent = await SoftDeleteParent.objects.all().only("id", "name").get(pk=parent.pk)
    await partial_parent.delete()

    assert (await SoftDeleteParent.objects.include_deleted().get(pk=parent.pk)).deleted_at is not None
    assert (await SoftDeleteChildCascadeSoft.objects.include_deleted().get(pk=child.pk)).deleted_at is not None


@pytest.mark.asyncio
async def test_soft_delete_and_restore_keep_version_and_auto_now_clean(db):
    created = await SoftDeleteVersionedDirtyTracked.objects.create(name="A")
    obj = await SoftDeleteVersionedDirtyTracked.objects.get(pk=created.pk)

    await obj.delete()
    assert obj.get_dirty_fields() == {}
    stored = await SoftDeleteVersionedDirtyTracked.objects.include_deleted().get(pk=obj.pk)
    assert (stored.version, stored.modified) == (obj.version, obj.modified)

    await obj.restore()
    assert obj.get_dirty_fields() == {}

    obj.name = "B"
    await obj.delete()
    assert obj.get_dirty_fields() == {"name": ("A", "B")}


# ============================================================================
# Writes that bypass delete()/restore(), stale and concurrent deletes
# ============================================================================


@pytest.mark.asyncio
async def test_save_update_fields_rejects_soft_delete_field(db):
    """save(update_fields=[soft_delete_field]) on a detached instance soft-deleted the row without
    its cascade."""
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="C", parent=parent)
    detached = SoftDeleteParent(id=parent.pk, name="P", deleted_at=Timezone.now())

    with pytest.raises(QueryError, match="deleted_at"):
        await detached.save(update_fields=["deleted_at"])

    assert await SoftDeleteParent.objects.filter(pk=parent.pk).exists()
    assert await SoftDeleteChildCascadeSoft.objects.filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_upsert_rejects_soft_delete_field_in_update_fields(db):
    """bulk_create(update_fields=[soft_delete_field]) soft-deleted a conflicting row without its
    cascade, or restored a deleted one behind restore()'s back."""
    thing = await SoftDeleteVersionedDirtyTracked.objects.create(name="A")
    await thing.delete()

    with pytest.raises(QueryError, match="deleted_at"):
        await SoftDeleteVersionedDirtyTracked.objects.bulk_create(
            [SoftDeleteVersionedDirtyTracked(id=thing.pk, name="B")],
            update_fields=["name", "deleted_at"],
            on_conflict=["id"],
        )

    assert not await SoftDeleteVersionedDirtyTracked.objects.filter(pk=thing.pk).exists()


@pytest.mark.asyncio
async def test_upsert_onto_a_soft_deleted_row_keeps_it_deleted(db):
    thing = await SoftDeleteVersionedDirtyTracked.objects.create(name="A")
    await thing.delete()
    deleted_at = (await SoftDeleteVersionedDirtyTracked.objects.include_deleted().get(pk=thing.pk)).deleted_at

    await SoftDeleteVersionedDirtyTracked.objects.bulk_create(
        [SoftDeleteVersionedDirtyTracked(id=thing.pk, name="B")], update_fields=["name"], on_conflict=["id"]
    )

    upserted = await SoftDeleteVersionedDirtyTracked.objects.include_deleted().get(pk=thing.pk)
    assert upserted.name == "B"
    assert upserted.deleted_at == deleted_at


@pytest.mark.asyncio
async def test_delete_of_a_stale_instance_keeps_the_original_deletion(db):
    """A copy read before the row was soft-deleted re-stamped deleted_at and re-ran the cascade."""
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeSoft.objects.create(name="C", parent=parent)
    stale_copy = await SoftDeleteParent.objects.get(pk=parent.pk)
    await parent.delete()
    parent_deleted_at = (await SoftDeleteParent.objects.include_deleted().get(pk=parent.pk)).deleted_at
    child_deleted_at = (await SoftDeleteChildCascadeSoft.objects.include_deleted().get(pk=child.pk)).deleted_at

    await stale_copy.delete()

    assert (await SoftDeleteParent.objects.include_deleted().get(pk=parent.pk)).deleted_at == parent_deleted_at
    assert (await SoftDeleteChildCascadeSoft.objects.include_deleted().get(pk=child.pk)).deleted_at == child_deleted_at
    assert stale_copy.deleted_at == parent_deleted_at


@pytest.mark.asyncio
async def test_delete_of_a_stale_versioned_instance_is_a_no_op(db):
    thing = await SoftDeleteVersioned.objects.create(name="A")
    stale_copy = await SoftDeleteVersioned.objects.get(pk=thing.pk)
    await thing.delete()

    await stale_copy.delete()

    fresh = await SoftDeleteVersioned.objects.include_deleted().get(pk=thing.pk)
    assert fresh.version == thing.version
    assert stale_copy.deleted_at == fresh.deleted_at


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_concurrent_deletes_of_the_same_rows_soft_delete_them_once(db_truncate):
    """Two concurrent deletes of the same live rows both counted them and cascaded twice - the
    second waits for the first's row lock now and finds them already deleted."""
    parents = [await SoftDeleteParent.objects.create(name=f"P{index}") for index in range(5)]
    children = [await SoftDeleteChildCascadeSoft.objects.create(name="C", parent=parent) for parent in parents]
    versioned = [await SoftDeleteVersioned.objects.create(name=f"V{index}") for index in range(5)]

    for parent in parents:
        counts = await asyncio.gather(
            SoftDeleteParent.objects.filter(pk=parent.pk).delete(),
            SoftDeleteParent.objects.filter(pk=parent.pk).delete(),
        )
        assert sorted(counts) == [0, 1]
    for thing in versioned:
        first_copy = await SoftDeleteVersioned.objects.get(pk=thing.pk)
        second_copy = await SoftDeleteVersioned.objects.get(pk=thing.pk)
        await asyncio.gather(first_copy.delete(), second_copy.delete())
        fresh = await SoftDeleteVersioned.objects.include_deleted().get(pk=thing.pk)
        assert fresh.version == thing.version + 1
        assert first_copy.deleted_at == second_copy.deleted_at == fresh.deleted_at

    for child in children:
        assert not await SoftDeleteChildCascadeSoft.objects.filter(pk=child.pk).exists()


@pytest.mark.asyncio
async def test_create_and_bulk_create_accept_the_soft_delete_and_optimistic_lock_fields(db):
    deleted_at = Timezone.now()
    created = await SoftDeleteVersioned.objects.create(name="imported", deleted_at=deleted_at, version=7)
    await SoftDeleteVersioned.objects.bulk_create(
        [SoftDeleteVersioned(name="bulk-imported", deleted_at=deleted_at, version=3)]
    )

    assert await SoftDeleteVersioned.objects.all().count() == 0
    rows = await SoftDeleteVersioned.objects.include_deleted().order_by("name").values_list("name", "version")
    assert rows == [("bulk-imported", 3), ("imported", 7)]
    with pytest.raises(QueryError):
        await SoftDeleteVersioned.objects.include_deleted().filter(pk=created.pk).update(deleted_at=None)
    with pytest.raises(QueryError):
        await SoftDeleteVersioned.objects.include_deleted().filter(pk=created.pk).update(version=1)


# ============================================================================
# Meta.soft_delete_hard_cascade
# ============================================================================


@pytest.mark.asyncio
async def test_delete_preview_of_a_default_soft_delete_keeps_hard_children_and_m2m_links(db):
    parent = await SoftDeleteParent.objects.create(name="P")
    child = await SoftDeleteChildCascadeHard.objects.create(name="C", parent=parent)
    m2m_parent = await SoftDeleteM2MParent.objects.create(name="M")
    await m2m_parent.peers.add(await SoftDeleteM2MPeer.objects.create(name="Peer"))

    parent_preview = await parent.delete_preview()
    m2m_preview = await m2m_parent.delete_preview()
    await parent.delete()
    await m2m_parent.delete()

    assert parent_preview.deleted == {}
    assert parent_preview.soft_deleted == {SoftDeleteParent: 1}
    assert m2m_preview.many_to_many_through == {}
    assert await SoftDeleteChildCascadeHard.objects.filter(pk=child.pk).exists()
    assert await SoftDeleteM2MParent.objects.include_deleted().filter(pk=m2m_parent.pk, peers__isnull=False).exists()


@pytest.mark.parametrize(
    ("meta_options", "message"),
    [
        ({"soft_delete_hard_cascade": True}, "needs Meta.soft_delete_field"),
        ({"soft_delete_field": "deleted_at", "soft_delete_hard_cascade": "yes"}, "must be a bool"),
    ],
    ids=["without_soft_delete_field", "not_a_bool"],
)
@pytest.mark.asyncio
async def test_soft_delete_hard_cascade_is_checked(meta_options, message):
    module_name = "tests._soft_delete_hard_cascade_models"
    meta = type("Meta", (), {"app": "models", **meta_options})
    model = type(
        "HardCascadeOption",
        (Model,),
        {
            "__module__": module_name,
            "id": fields.IntField(primary_key=True),
            "deleted_at": fields.DatetimeField(null=True),
            "Meta": meta,
        },
    )
    module = types.ModuleType(module_name)
    module.HardCascadeOption = model
    sys.modules[module_name] = module
    try:
        with pytest.raises(ConfigurationError, match=message):
            async with HareContext() as context:
                await context.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": [module_name]}))
    finally:
        sys.modules.pop(module_name, None)
