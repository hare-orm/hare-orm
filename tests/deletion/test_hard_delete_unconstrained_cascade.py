"""db_constraint=False means the schema generator never emits a real FK constraint for a
relation (see BaseSchemaGenerator) - so a real hard DELETE has nothing at the database level
enforcing that relation's on_delete either, unless Model.delete()/QuerySet.delete() run the same
Python-side cascade ReverseRelationCascade.apply_soft_delete_cascade() already runs
unconditionally for a soft-delete model. These tests cover the same CASCADE/SET_NULL/SET_DEFAULT/
RESTRICT/PROTECT matrix for a plain hard-delete model whose relations are unconstrained.
"""

import pytest

from hare.contrib.test import hare_test_context, requires_features
from hare.exceptions import IntegrityError, ProtectedError
from hare.transactions.transactions import Transactions
from tests.shared_models.testmodels import (
    Author,
    Book,
    HardDeleteUnconstrainedChildCascade,
    HardDeleteUnconstrainedChildProtect,
    HardDeleteUnconstrainedChildRestrict,
    HardDeleteUnconstrainedChildSetDefault,
    HardDeleteUnconstrainedChildSetNull,
    HardDeleteUnconstrainedM2MPeer,
    HardDeleteUnconstrainedParent,
    HardDeleteUnconstrainedParentWithSoftDeletedChildren,
    HardDeleteUnconstrainedSoftDeletedChildProtect,
    HardDeleteUnconstrainedSoftDeletedChildRestrict,
    TransitiveCascadeChildCascade,
    TransitiveCascadeChildProtect,
    TransitiveCascadeChildRestrict,
    TransitiveCascadeChildSetNull,
    TransitiveCascadeGrandparent,
    TransitiveCascadeParent,
)


@pytest.mark.asyncio
async def test_cascade_deletes_children_when_constraint_absent(db):
    parent = await HardDeleteUnconstrainedParent.objects.create(name="P")
    child = await HardDeleteUnconstrainedChildCascade.objects.create(name="C", parent=parent)

    await parent.delete()

    assert not await HardDeleteUnconstrainedParent.objects.filter(pk=parent.pk).exists()
    assert not await HardDeleteUnconstrainedChildCascade.objects.filter(pk=child.pk).exists()


@pytest.mark.parametrize(
    ("child_class", "expected_parent_id"),
    [
        pytest.param(HardDeleteUnconstrainedChildSetNull, None, id="set_null_clears_children_when_constraint_absent"),
        pytest.param(
            HardDeleteUnconstrainedChildSetDefault, 999, id="set_default_resets_children_when_constraint_absent"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_child_is_reset_when_constraint_absent(db, child_class, expected_parent_id):
    parent = await HardDeleteUnconstrainedParent.objects.create(name="P")
    child = await child_class.objects.create(name="C", parent=parent)

    await parent.delete()

    refreshed = await child_class.objects.get(pk=child.pk)
    assert refreshed.parent_id == expected_parent_id


@pytest.mark.parametrize(
    ("child_class", "exception_class", "delete"),
    [
        pytest.param(
            HardDeleteUnconstrainedChildRestrict,
            IntegrityError,
            lambda parent: parent.delete(),
            id="restrict_blocks_delete_when_constraint_absent",
        ),
        # PROTECT is checked unconditionally (ReverseRelationCascade.check_protected), regardless of
        # db_constraint - a regression guard that this bugfix didn't accidentally change that.
        pytest.param(
            HardDeleteUnconstrainedChildProtect,
            ProtectedError,
            lambda parent: parent.delete(),
            id="protect_still_blocks_delete_when_constraint_absent",
        ),
        pytest.param(
            HardDeleteUnconstrainedChildRestrict,
            IntegrityError,
            lambda parent: type(parent).objects.filter(pk=parent.pk).delete(),
            id="bulk_queryset_delete_respects_restrict_when_constraint_absent",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_blocking_child_keeps_its_parent_when_constraint_absent(db, child_class, exception_class, delete):
    parent = await HardDeleteUnconstrainedParent.objects.create(name="P")
    await child_class.objects.create(name="C", parent=parent)

    with pytest.raises(exception_class):
        await delete(parent)

    assert await HardDeleteUnconstrainedParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.parametrize(
    ("child_class", "exception_class", "delete"),
    [
        pytest.param(
            HardDeleteUnconstrainedSoftDeletedChildRestrict,
            IntegrityError,
            lambda parent: parent.delete(),
            id="restrict_blocks_delete_even_when_the_only_referencing_row_is_soft_deleted",
        ),
        # check_protected().
        pytest.param(
            HardDeleteUnconstrainedSoftDeletedChildProtect,
            ProtectedError,
            lambda parent: parent.delete(),
            id="protect_blocks_delete_even_when_the_only_referencing_row_is_soft_deleted",
        ),
        # The bulk QuerySet.delete() path - ReverseRelationCascade.check_protected_bulk()'s own
        # FK-PROTECT loop instead of the single-instance check_protected().
        pytest.param(
            HardDeleteUnconstrainedSoftDeletedChildProtect,
            ProtectedError,
            lambda parent: type(parent).objects.filter(pk=parent.pk).delete(),
            id="bulk_queryset_delete_respects_protect_even_when_the_only_referencing_row_is_soft_deleted",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_blocking_child_blocks_even_when_the_only_referencing_row_is_soft_deleted(
    db, child_class, exception_class, delete
):
    """The referencing child row here has its own Meta.soft_delete_field - once it's been
    soft-deleted, it's still physically present with its FK still pointing at the parent, but a
    plain .filter()/.exists() against it would no longer see it through its own ambient
    soft-delete scope. RESTRICT's and PROTECT's own existence pre-checks must still find it -
    db_constraint=False means nothing else backstops this at the database level."""
    parent = await HardDeleteUnconstrainedParentWithSoftDeletedChildren.objects.create(name="P")
    child = await child_class.objects.create(name="C", parent=parent)
    await child.delete()

    with pytest.raises(exception_class):
        await delete(parent)

    assert await HardDeleteUnconstrainedParentWithSoftDeletedChildren.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_m2m_cascade_clears_through_rows_when_constraint_absent(db):
    parent = await HardDeleteUnconstrainedParent.objects.create(name="P")
    peer = await HardDeleteUnconstrainedM2MPeer.objects.create(name="Peer")
    await parent.peers.add(peer)

    await parent.delete()

    assert await HardDeleteUnconstrainedM2MPeer.objects.filter(pk=peer.pk).exists()
    assert not await peer.parents.all()


@pytest.mark.parametrize(
    "delete",
    [
        pytest.param(lambda parent: parent.delete(), id="m2m_protect_still_blocks_delete_when_constraint_absent"),
        pytest.param(
            lambda parent: type(parent).objects.filter(pk=parent.pk).delete(),
            id="bulk_queryset_delete_respects_m2m_protect_when_constraint_absent",
        ),
    ],
)
@pytest.mark.asyncio
async def test_m2m_protect_blocks_delete_when_constraint_absent(db, delete):
    """PROTECT on a ManyToManyField is checked unconditionally (ReverseRelationCascade.
    check_protected/check_protected_bulk), regardless of db_constraint - mirrors the FK PROTECT
    regression guard above."""
    parent = await HardDeleteUnconstrainedParent.objects.create(name="P")
    peer = await HardDeleteUnconstrainedM2MPeer.objects.create(name="Peer")
    await parent.protected_peers.add(peer)

    with pytest.raises(ProtectedError):
        await delete(parent)

    assert await HardDeleteUnconstrainedParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_queryset_delete_cascades_when_constraint_absent(db):
    parent1 = await HardDeleteUnconstrainedParent.objects.create(name="P1")
    parent2 = await HardDeleteUnconstrainedParent.objects.create(name="P2")
    child1 = await HardDeleteUnconstrainedChildCascade.objects.create(name="C1", parent=parent1)
    child2 = await HardDeleteUnconstrainedChildCascade.objects.create(name="C2", parent=parent2)

    deleted_count = await HardDeleteUnconstrainedParent.objects.filter(pk__in=[parent1.pk, parent2.pk]).delete()

    assert deleted_count == 2
    assert not await HardDeleteUnconstrainedChildCascade.objects.filter(pk__in=[child1.pk, child2.pk]).exists()


@pytest.mark.asyncio
async def test_bulk_queryset_delete_is_atomic_when_a_later_row_is_restricted(db_truncate):
    """QuerySet.delete() deletes each matching row through its own Model.delete() when a relation
    is unconstrained - each opening its own transaction, so a RESTRICT failure on a later row used
    to leave every earlier row already deleted, unlike the single native DELETE (all rows or
    none)."""
    first_parent = await HardDeleteUnconstrainedParent.objects.create(name="first")
    second_parent = await HardDeleteUnconstrainedParent.objects.create(name="second")
    restricted_parent = await HardDeleteUnconstrainedParent.objects.create(name="restricted")
    cascade_child = await HardDeleteUnconstrainedChildCascade.objects.create(name="child", parent=first_parent)
    await HardDeleteUnconstrainedChildRestrict.objects.create(name="blocker", parent=restricted_parent)

    with pytest.raises(IntegrityError):
        await HardDeleteUnconstrainedParent.objects.all().delete()

    remaining_parent_pks = {parent.pk async for parent in HardDeleteUnconstrainedParent.objects.all()}
    assert remaining_parent_pks == {first_parent.pk, second_parent.pk, restricted_parent.pk}
    assert await HardDeleteUnconstrainedChildCascade.objects.filter(pk=cascade_child.pk).exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_bulk_queryset_delete_failure_rolls_back_only_to_its_own_savepoint_inside_an_outer_transaction(
    db_truncate,
):
    """Inside a caller's transaction the shared wrapper is a savepoint - a failed bulk delete
    undoes just its own partial work and leaves the outer transaction usable, with the caller's
    earlier writes intact."""
    first_parent = await HardDeleteUnconstrainedParent.objects.create(name="first")
    restricted_parent = await HardDeleteUnconstrainedParent.objects.create(name="restricted")
    await HardDeleteUnconstrainedChildRestrict.objects.create(name="blocker", parent=restricted_parent)

    async with Transactions.atomic():
        marker = await HardDeleteUnconstrainedM2MPeer.objects.create(name="marker")
        with pytest.raises(IntegrityError):
            await HardDeleteUnconstrainedParent.objects.all().delete()
        await HardDeleteUnconstrainedM2MPeer.objects.filter(pk=marker.pk).update(name="marker-updated")

    assert (await HardDeleteUnconstrainedM2MPeer.objects.get(pk=marker.pk)).name == "marker-updated"
    assert await HardDeleteUnconstrainedParent.objects.filter(pk=first_parent.pk).exists()
    assert await HardDeleteUnconstrainedParent.objects.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_queryset_delete_of_several_rows_when_constraint_absent_still_succeeds(db_truncate):
    """The shared transaction doesn't change the success path - every matching row is deleted
    with its cascade applied, and the count is returned."""
    parents = [await HardDeleteUnconstrainedParent.objects.create(name=f"P{index}") for index in range(3)]
    for parent in parents:
        await HardDeleteUnconstrainedChildCascade.objects.create(name=f"child-of-{parent.name}", parent=parent)

    deleted_count = await HardDeleteUnconstrainedParent.objects.all().delete()

    assert deleted_count == 3
    assert await HardDeleteUnconstrainedParent.objects.all().count() == 0
    assert await HardDeleteUnconstrainedChildCascade.objects.all().count() == 0


@pytest.mark.asyncio
async def test_constrained_relation_delete_unaffected(db):
    """Sanity check that db_constraint=True relations (the common case) still hard-delete via the
    fast single-DELETE-statement path, unaffected by the new unconstrained-relation branch -
    Book/Author's FK is db_constraint=True (the default) and on_delete=CASCADE."""
    author = await Author.objects.create(name="A")
    book = await Book.objects.create(name="B", author=author, rating=4.0)

    await author.delete()

    assert not await Author.objects.filter(pk=author.pk).exists()
    assert not await Book.objects.filter(pk=book.pk).exists()


# The relations below all describe TransitiveCascadeGrandparent -> TransitiveCascadeParent (a
# real, db_constraint=True FK CASCADE - the database's own ON DELETE CASCADE physically deletes
# TransitiveCascadeParent rows, not Python) -> a TransitiveCascadeChild* (db_constraint=False, no
# real FK constraint at all). ReverseRelationCascade.has_unconstrained_relations()/
# _apply_cascade_one() used to only ever look at the relations declared directly on the model
# actually being deleted - a db_constraint=True CASCADE relation was never walked into at all, so
# TransitiveCascadeParent's own db_constraint=False children were completely invisible: never
# PROTECT/RESTRICT-checked, never CASCADE-deleted/SET_NULL-ed, not even an error, just silently
# unhandled once the grandparent's real DELETE cascaded past the parent.


@pytest.mark.asyncio
async def test_cascade_deletes_grandchild_reached_through_a_constrained_cascade_hop(db):
    top = await TransitiveCascadeGrandparent.objects.create(name="top")
    mid = await TransitiveCascadeParent.objects.create(name="mid", grandparent=top)
    leaf = await TransitiveCascadeChildCascade.objects.create(name="leaf", parent=mid)

    await top.delete()

    assert not await TransitiveCascadeGrandparent.objects.filter(pk=top.pk).exists()
    assert not await TransitiveCascadeParent.objects.filter(pk=mid.pk).exists()
    assert not await TransitiveCascadeChildCascade.objects.filter(pk=leaf.pk).exists()


@pytest.mark.asyncio
async def test_set_null_clears_grandchild_reached_through_a_constrained_cascade_hop(db):
    top = await TransitiveCascadeGrandparent.objects.create(name="top")
    mid = await TransitiveCascadeParent.objects.create(name="mid", grandparent=top)
    leaf = await TransitiveCascadeChildSetNull.objects.create(name="leaf", parent=mid)

    await top.delete()

    refreshed = await TransitiveCascadeChildSetNull.objects.get(pk=leaf.pk)
    assert refreshed.parent_id is None


@pytest.mark.parametrize(
    ("child_class", "exception_class", "delete"),
    [
        pytest.param(
            TransitiveCascadeChildRestrict,
            IntegrityError,
            lambda parent: parent.delete(),
            id="restrict_blocks_delete_of_grandchild_reached_through_a_constrained_cascade_hop",
        ),
        pytest.param(
            TransitiveCascadeChildProtect,
            ProtectedError,
            lambda parent: parent.delete(),
            id="protect_blocks_delete_of_grandchild_reached_through_a_constrained_cascade_hop",
        ),
        pytest.param(
            TransitiveCascadeChildProtect,
            ProtectedError,
            lambda parent: type(parent).objects.filter(pk=parent.pk).delete(),
            id="bulk_queryset_delete_respects_protect_through_a_constrained_cascade_hop",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_blocking_grandchild_reached_through_a_constrained_cascade_hop_blocks_delete(
    db, child_class, exception_class, delete
):
    top = await TransitiveCascadeGrandparent.objects.create(name="top")
    mid = await TransitiveCascadeParent.objects.create(name="mid", grandparent=top)
    await child_class.objects.create(name="leaf", parent=mid)

    with pytest.raises(exception_class):
        await delete(top)

    assert await TransitiveCascadeGrandparent.objects.filter(pk=top.pk).exists()
    assert await TransitiveCascadeParent.objects.filter(pk=mid.pk).exists()


@pytest.mark.asyncio
async def test_bulk_queryset_delete_cascades_through_a_constrained_cascade_hop(db):
    top1 = await TransitiveCascadeGrandparent.objects.create(name="top1")
    top2 = await TransitiveCascadeGrandparent.objects.create(name="top2")
    mid1 = await TransitiveCascadeParent.objects.create(name="mid1", grandparent=top1)
    mid2 = await TransitiveCascadeParent.objects.create(name="mid2", grandparent=top2)
    leaf1 = await TransitiveCascadeChildCascade.objects.create(name="leaf1", parent=mid1)
    leaf2 = await TransitiveCascadeChildCascade.objects.create(name="leaf2", parent=mid2)

    deleted_count = await TransitiveCascadeGrandparent.objects.filter(pk__in=[top1.pk, top2.pk]).delete()

    assert deleted_count == 2
    assert not await TransitiveCascadeParent.objects.filter(pk__in=[mid1.pk, mid2.pk]).exists()
    assert not await TransitiveCascadeChildCascade.objects.filter(pk__in=[leaf1.pk, leaf2.pk]).exists()


@pytest.mark.sqlite_only
@pytest.mark.asyncio
async def test_a_delete_while_a_migration_switched_foreign_keys_off_cascades_in_python(tmp_path):
    """A SQLite migration runs with PRAGMA foreign_keys off - the database cascades nothing then, so
    a delete in its RunPython carries out on_delete itself."""
    async with hare_test_context(
        modules=["tests.shared_models.testmodels"],
        db_url=f"sqlite+aiosqlite://{(tmp_path / 'cascade.sqlite3').as_posix()}",
    ):
        connection = Author.get_connection(for_write=True)
        editor = connection.dialect.schema_editor_class(connection, atomic=False, collect_sql=False)
        first_author = await Author.objects.create(name="first")
        second_author = await Author.objects.create(name="second")
        await Book.objects.create(name="a", author=first_author, rating=1)
        await Book.objects.create(name="b", author=second_author, rating=1)
        async with editor.constraint_checking_disabled():
            await first_author.delete()
            await Author.objects.filter(pk=second_author.pk).delete()
            assert await Book.objects.count() == 0
        assert connection.enforces_foreign_keys()
