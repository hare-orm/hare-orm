"""A chain of thousands of self-referential on_delete=CASCADE rows, all sharing
Meta.soft_delete_field - reproduces Finding 4: ReverseRelationCascade's old recursive cascade
walk (one nested `await related_obj.delete()` per level, regardless of db_constraint) overflowed
Python's own call stack for a chain this deep. Walked iteratively now (see hare/cascade.py)."""

import pytest

from hare.contrib.test import requires_features
from hare.dialects.sqlite.exceptions import SqliteTriggerRecursionLimitError
from hare.exceptions import CascadeDepthLimitError, IntegrityError, StaleObjectError
from tests.testmodels import (
    DiamondCascadeChildB,
    DiamondCascadeChildC,
    DiamondCascadeSharedDescendant,
    DiamondCascadeTop,
    HardDeleteChainWithRestrictEdge,
    HardDeleteSelfReferentialChain,
    SoftDeleteSelfReferentialChain,
    Tournament,
)

CHAIN_LENGTH = 1500

#: Comfortably past SQLite's own SQLITE_LIMIT_TRIGGER_DEPTH (1000 by default, confirmed by direct
#: binary search not raisable past that compiled ceiling via sqlite3.Connection.setlimit()).
HARD_DELETE_CHAIN_LENGTH = 1500

#: Both restrict-edge node ids below must clear HARD_DELETE_CHAIN_LENGTH's own 1000-deep ceiling
#: (with a clear gap between them) for the two restrict-edge tests to reach the SQLite recursion
#: fallback at all - see those tests' own docstrings for why.
RESTRICT_EDGE_DEEPER_NODE_ID = 1400
RESTRICT_EDGE_SHALLOWER_NODE_ID = 1100


async def _create_chain(length: int) -> None:
    rows = [SoftDeleteSelfReferentialChain(id=1, name="row-1")]
    rows.extend(
        SoftDeleteSelfReferentialChain(id=row_id, name=f"row-{row_id}", parent_id=row_id - 1)
        for row_id in range(2, length + 1)
    )
    await SoftDeleteSelfReferentialChain.objects.bulk_create(rows)


async def _create_hard_delete_chain(length: int) -> None:
    rows = [HardDeleteSelfReferentialChain(id=1, name="row-1")]
    rows.extend(
        HardDeleteSelfReferentialChain(id=row_id, name=f"row-{row_id}", parent_id=row_id - 1)
        for row_id in range(2, length + 1)
    )
    await HardDeleteSelfReferentialChain.objects.bulk_create(rows)


async def _create_hard_delete_chain_with_restrict_edge(length: int) -> None:
    rows = [HardDeleteChainWithRestrictEdge(id=1, name="row-1")]
    rows.extend(
        HardDeleteChainWithRestrictEdge(id=row_id, name=f"row-{row_id}", parent_id=row_id - 1)
        for row_id in range(2, length + 1)
    )
    await HardDeleteChainWithRestrictEdge.objects.bulk_create(rows)


@pytest.mark.asyncio
async def test_deep_self_referential_cascade_does_not_overflow_stack(db):
    await _create_chain(CHAIN_LENGTH)

    root = await SoftDeleteSelfReferentialChain.objects.get(id=1)
    await root.delete()  # used to raise RecursionError before Finding 4's fix

    live_count = await SoftDeleteSelfReferentialChain.objects.all().count()
    assert live_count == 0

    all_rows = await SoftDeleteSelfReferentialChain.objects.include_deleted().all()
    assert len(all_rows) == CHAIN_LENGTH
    assert all(row.deleted_at is not None for row in all_rows)


@pytest.mark.asyncio
async def test_deep_self_referential_cascade_from_bulk_queryset_delete(db):
    """QuerySet.delete()'s own bulk soft-delete fallback (_delete_each_matching_instance) also
    calls Model.delete() per matching row, so it shares the exact same cascade walk."""
    await _create_chain(CHAIN_LENGTH)

    deleted_count = await SoftDeleteSelfReferentialChain.objects.filter(id=1).delete()

    assert deleted_count == 1
    all_rows = await SoftDeleteSelfReferentialChain.objects.include_deleted().all()
    assert len(all_rows) == CHAIN_LENGTH
    assert all(row.deleted_at is not None for row in all_rows)


@pytest.mark.asyncio
async def test_shallow_self_referential_cascade_still_works(db):
    """A small chain still soft-deletes correctly and in full, same as before Finding 4's fix
    made the walk iterative."""
    await _create_chain(5)

    root = await SoftDeleteSelfReferentialChain.objects.get(id=1)
    await root.delete()

    all_rows = await SoftDeleteSelfReferentialChain.objects.include_deleted().all()
    assert len(all_rows) == 5
    assert all(row.deleted_at is not None for row in all_rows)


@pytest.mark.asyncio
async def test_diamond_cascade_does_not_double_process_shared_descendant(db):
    """DiamondCascadeTop CASCADEs onto two children (ChildB/ChildC) that both, in the SAME wave,
    CASCADE onto the SAME DiamondCascadeSharedDescendant row - the frontier-based walk used to
    queue and persist that shared row twice, and since it has both soft_delete_field and
    optimistic_lock_field, the second persist saw its own already-bumped version and raised
    StaleObjectError, rolling back the whole cascade even though nothing was concurrently
    modified."""
    top = await DiamondCascadeTop.objects.create(name="top")
    child_b = await DiamondCascadeChildB.objects.create(name="b", top=top)
    child_c = await DiamondCascadeChildC.objects.create(name="c", top=top)
    shared = await DiamondCascadeSharedDescendant.objects.create(name="shared", via_b=child_b, via_c=child_c)

    await top.delete()  # used to raise StaleObjectError before frontier dedup

    assert not await DiamondCascadeTop.objects.filter(pk=top.pk).exists()
    assert not await DiamondCascadeChildB.objects.filter(pk=child_b.pk).exists()
    assert not await DiamondCascadeChildC.objects.filter(pk=child_c.pk).exists()
    assert not await DiamondCascadeSharedDescendant.objects.filter(pk=shared.pk).exists()

    shared_after = await DiamondCascadeSharedDescendant.objects.include_deleted().get(pk=shared.pk)
    assert shared_after.deleted_at is not None
    assert shared_after.version == 1  # bumped exactly once, not twice


@pytest.mark.asyncio
async def test_diamond_cascade_stale_delete_still_raises_for_a_real_concurrent_change(db):
    """The frontier dedup must not swallow a genuine concurrent modification - only a row
    rediscovered within the SAME cascade walk is deduplicated, not a row independently changed by
    something else entirely."""
    shared = await DiamondCascadeSharedDescendant.objects.create(
        name="shared",
        via_b=await DiamondCascadeChildB.objects.create(
            name="b", top=await DiamondCascadeTop.objects.create(name="top")
        ),
        via_c=await DiamondCascadeChildC.objects.create(
            name="c", top=await DiamondCascadeTop.objects.create(name="top2")
        ),
    )

    live_copy = await DiamondCascadeSharedDescendant.objects.get(pk=shared.pk)
    live_copy.name = "renamed"
    await live_copy.save()  # bumps version out from under the stale `shared` instance below

    with pytest.raises(StaleObjectError):
        await shared.delete()


@pytest.mark.asyncio
async def test_deep_hard_delete_self_referential_cascade_succeeds(db):
    """HardDeleteSelfReferentialChain has a real DB-level FK constraint (db_constraint=True, the
    default) and no Meta.soft_delete_field - a real DELETE relies entirely on the database's own
    native ON DELETE CASCADE. On SQLite, that recurses past SQLITE_LIMIT_TRIGGER_DEPTH (1000 by
    default) for a chain this deep and used to raise SqliteTriggerRecursionLimitError with the
    database left in a partially-cascaded state (confirmed live: a plain, unwrapped DELETE is not
    atomic against this specific failure). Model.delete() now retries with a Python-side cascade
    walk instead (ReverseRelationCascade.apply_hard_delete_cascade_for_all_relations)."""
    await _create_hard_delete_chain(HARD_DELETE_CHAIN_LENGTH)

    root = await HardDeleteSelfReferentialChain.objects.get(id=1)
    await root.delete()

    assert await HardDeleteSelfReferentialChain.objects.all().count() == 0


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_raw_delete_past_the_native_cascade_depth_raises_cascade_depth_limit_error(db):
    """The SQLite client reports a native cascade stopped at SQLITE_LIMIT_TRIGGER_DEPTH as its
    SqliteTriggerRecursionLimitError, a CascadeDepthLimitError - the dialect-neutral exception
    Model.delete()/QuerySet.delete() catch to finish the cascade in Python. A raw DELETE gets no
    such retry and raises it as is."""
    await _create_hard_delete_chain(HARD_DELETE_CHAIN_LENGTH)
    connection = HardDeleteSelfReferentialChain.get_connection(for_write=True)
    table = HardDeleteSelfReferentialChain._meta.db_table

    with pytest.raises(CascadeDepthLimitError) as exc_info:
        await connection.execute(f'DELETE FROM "{table}" WHERE "id" = ?', [1])

    assert isinstance(exc_info.value, SqliteTriggerRecursionLimitError)


@pytest.mark.asyncio
async def test_deep_hard_delete_cascade_from_bulk_queryset_delete(db):
    """QuerySet.delete()'s own bulk hard-delete path (DeleteQuery._execute()) issues the exact
    same type of single native DELETE relying on the database's own cascade, and shares the same
    SQLite fallback via _delete_each_matching_instance()."""
    await _create_hard_delete_chain(HARD_DELETE_CHAIN_LENGTH)

    deleted_count = await HardDeleteSelfReferentialChain.objects.filter(id=1).delete()

    assert deleted_count == 1
    assert await HardDeleteSelfReferentialChain.objects.all().count() == 0


@pytest.mark.asyncio
async def test_shallow_hard_delete_self_referential_cascade_still_works(db):
    """A small chain still hard-deletes correctly and in full - the SQLite-only cascade-cycle gate
    (ReverseRelationCascade.has_self_cascading_constrained_relations) and its fallback machinery
    must not change behavior for the overwhelmingly common shallow case."""
    await _create_hard_delete_chain(5)

    root = await HardDeleteSelfReferentialChain.objects.get(id=1)
    await root.delete()

    assert await HardDeleteSelfReferentialChain.objects.all().count() == 0


@pytest.mark.asyncio
async def test_hard_delete_of_a_model_with_no_cascade_cycle_still_works(db):
    """Tournament has no backward relations at all, so it can never hit
    SQLITE_LIMIT_TRIGGER_DEPTH regardless of row count - has_self_cascading_constrained_relations
    must gate it away from the transaction-wrapped retry path entirely (confirmed separately via
    tests/test_query_counter.py, which pins this to a specific query count)."""
    tournament = await Tournament.objects.create(name="T")
    await tournament.delete()

    assert not await Tournament.objects.filter(pk=tournament.pk).exists()


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_deep_chain_with_restrict_edge_from_a_deeper_node_to_a_shallower_one_succeeds(db):
    """A separate on_delete=RESTRICT relation between two nodes of the SAME deep CASCADE tree used
    to false-positive with IntegrityError inside apply_hard_delete_cascade_for_all_relations's
    bottom_up_persist walk, because the RESTRICT check ran at discovery time (top-down) while the
    blocking row - itself part of the same tree, about to be removed by this same operation - was
    still physically present. Fixed via restrict_check_exclude/_collect_cascade_reachable_keys.

    SQLite-only: this exercises the SQLite-specific bottom_up_persist fallback and its own
    specific bottom-up ordering guarantee - confirmed live that Postgres's native CASCADE+RESTRICT
    interaction (which this code never touches, since Postgres has no SQLITE_LIMIT_TRIGGER_DEPTH
    to work around) does NOT share this same directional behavior at all: the identical schema and
    row shape raises IntegrityError on Postgres for BOTH directions, not just the "unfavorable"
    one below - Postgres validates FK constraints against every row's original pre-delete state,
    not against however far its own cascade has progressed.

    Both nodes must sit past SQLite's own ~1000-deep native recursion abort for this to even
    reach the Python fallback at all - a RESTRICT edge entirely within the first ~1000 levels (or
    pointing at the row being explicitly deleted) is checked by SQLite's own native cascade before
    it ever gets deep enough to hit SQLITE_LIMIT_TRIGGER_DEPTH, so it fails (correctly) on the very
    first, plain native attempt and never reaches this fallback either way.

    This is the direction the fix can fully resolve: the deeper node restricts the shallower one
    - bottom-up persist deletes the deeper node FIRST regardless, so by the time the shallower
    node's own real DELETE runs, the row that restricted it is already gone and the database's
    own real FK constraint is satisfied too, not just the Python-level check."""
    await _create_hard_delete_chain_with_restrict_edge(HARD_DELETE_CHAIN_LENGTH)
    referencer = await HardDeleteChainWithRestrictEdge.objects.get(id=RESTRICT_EDGE_DEEPER_NODE_ID)
    referencer.restrictor_id = RESTRICT_EDGE_SHALLOWER_NODE_ID
    await referencer.save()

    root = await HardDeleteChainWithRestrictEdge.objects.get(id=1)
    await root.delete()

    assert await HardDeleteChainWithRestrictEdge.objects.all().count() == 0


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_deep_chain_with_restrict_edge_from_a_shallower_node_to_a_deeper_one_still_raises(db):
    """The opposite direction from the test above - the shallower node restricts the deeper one -
    is a documented, deliberate limitation, not a bug: bottom_up_persist deletes the deeper node
    FIRST (deepest first, to avoid re-triggering SQLite's own native cascade recursion - see
    apply_hard_delete_cascade_for_all_relations's own docstring), but the shallower node (the row
    whose live FK is what actually blocks deleting the deeper one) is still present at that
    point, since it persists later. The database's own real RESTRICT constraint correctly rejects
    this ordering - no ordering this fallback could choose can satisfy both SQLITE_LIMIT_TRIGGER_DEPTH
    avoidance (which needs bottom-up) and this specific RESTRICT edge (which needs the referencing
    row gone before the row it references) at once."""
    await _create_hard_delete_chain_with_restrict_edge(HARD_DELETE_CHAIN_LENGTH)
    referencer = await HardDeleteChainWithRestrictEdge.objects.get(id=RESTRICT_EDGE_SHALLOWER_NODE_ID)
    referencer.restrictor_id = RESTRICT_EDGE_DEEPER_NODE_ID
    await referencer.save()

    root = await HardDeleteChainWithRestrictEdge.objects.get(id=1)
    with pytest.raises(IntegrityError):
        await root.delete()


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_bulk_delete_sqlite_recursion_fallback_is_atomic_when_a_later_row_is_restricted(db):
    """A bulk DELETE whose native cascade exceeds SQLITE_LIMIT_TRIGGER_DEPTH rolls back and
    retries row by row (DeleteQuery._delete_each_matching_instance) - each row in its own
    transaction, so when a later root's own cascade hit a RESTRICT edge the deep chain deleted
    before it stayed deleted, unlike the single atomic DELETE the caller asked for. Root 1 is a
    chain past the recursion limit; root 4000's child (4001) is restricted by the unrelated row
    5000, so deleting both roots must fail as a whole."""
    await _create_hard_delete_chain_with_restrict_edge(HARD_DELETE_CHAIN_LENGTH)
    await HardDeleteChainWithRestrictEdge.objects.bulk_create(
        [
            HardDeleteChainWithRestrictEdge(id=4000, name="row-4000"),
            HardDeleteChainWithRestrictEdge(id=4001, name="row-4001", parent_id=4000),
            HardDeleteChainWithRestrictEdge(id=5000, name="row-5000", restrictor_id=4001),
        ]
    )

    with pytest.raises(IntegrityError):
        await HardDeleteChainWithRestrictEdge.objects.filter(id__in=[1, 4000]).delete()

    assert await HardDeleteChainWithRestrictEdge.objects.all().count() == HARD_DELETE_CHAIN_LENGTH + 3
