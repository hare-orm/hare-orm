import asyncio
import os

import pytest
import pytest_asyncio

from hare.contrib import test as hare_test
from hare.contrib.test import requires_features
from hare.exceptions import (
    ConfigurationError,
    IntegrityError,
    QueryError,
    StaleObjectError,
)
from hare.models.class_building.model_definition_checks import ModelDefinitionChecks
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    IntFields,
    VersionedComposite,
    VersionedDirtyTracked,
    VersionedThing,
    VersionedUnique,
    VersionedUniqueAutoNow,
)
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
async def test_create_starts_at_default_version(db):
    thing = await VersionedThing.objects.create(name="A")
    assert thing.version == 0


@pytest.mark.asyncio
async def test_save_increments_version(db):
    thing = await VersionedThing.objects.create(name="A")
    thing.name = "B"
    await thing.save()
    assert thing.version == 1

    thing.name = "C"
    await thing.save()
    assert thing.version == 2


@pytest.mark.asyncio
async def test_save_persists_incremented_version(db):
    thing = await VersionedThing.objects.create(name="A")
    thing.name = "B"
    await thing.save()

    fresh = await VersionedThing.objects.get(pk=thing.pk)
    assert fresh.version == 1
    assert fresh.name == "B"


@pytest.mark.asyncio
async def test_concurrent_save_raises_stale_object_error(db):
    original = await VersionedThing.objects.create(name="A")
    copy_1 = await VersionedThing.objects.get(pk=original.pk)
    copy_2 = await VersionedThing.objects.get(pk=original.pk)

    copy_1.name = "From copy 1"
    await copy_1.save()

    copy_2.name = "From copy 2"
    with pytest.raises(StaleObjectError):
        await copy_2.save()

    # copy_1's write won - not overwritten by the failed copy_2 save
    final = await VersionedThing.objects.get(pk=original.pk)
    assert final.name == "From copy 1"
    assert final.version == 1


@pytest.mark.asyncio
async def test_stale_object_error_carries_structured_conflict_data(db):
    """`StaleObjectError` must expose `.model`/`.pk`/`.expected_version` programmatically - a
    consumer (e.g. an admin panel showing "you edited version 3, the database now has version 5")
    shouldn't have to regex-parse `.args[0]`'s formatted message."""
    original = await VersionedThing.objects.create(name="A")
    stale_copy = await VersionedThing.objects.get(pk=original.pk)

    original.name = "From original"
    await original.save()

    stale_copy.name = "From stale copy"
    with pytest.raises(StaleObjectError) as excinfo:
        await stale_copy.save()

    error = excinfo.value
    assert error.model is VersionedThing
    assert error.pk == original.pk
    assert error.expected_version == 0


@pytest.mark.asyncio
async def test_stale_object_error_is_picklable(db):
    """Mirrors `ProtectedError`'s own reason for a custom `__reduce__` - the default, args-only
    `BaseException.__reduce__` can't reconstruct this exception's `model`/`pk`/`expected_version`
    constructor arguments, so pickling (e.g. sending it across a process boundary, or a test
    framework re-raising it) used to raise `TypeError` on unpickling."""
    import pickle

    original = await VersionedThing.objects.create(name="A")
    stale_copy = await VersionedThing.objects.get(pk=original.pk)
    original.name = "From original"
    await original.save()
    stale_copy.name = "From stale copy"

    with pytest.raises(StaleObjectError) as excinfo:
        await stale_copy.save()

    reconstructed = pickle.loads(pickle.dumps(excinfo.value))
    assert reconstructed.model is VersionedThing
    assert reconstructed.pk == original.pk
    assert reconstructed.expected_version == 0
    assert str(reconstructed) == str(excinfo.value)


@pytest.mark.asyncio
async def test_stale_save_rolls_back_in_memory_version(db):
    """The failed save must not leave the instance's in-memory version bumped past what's
    actually in the DB - otherwise a caller inspecting .version after catching the error would
    see a number that was never actually persisted."""
    original = await VersionedThing.objects.create(name="A")
    copy_1 = await VersionedThing.objects.get(pk=original.pk)
    copy_2 = await VersionedThing.objects.get(pk=original.pk)

    await copy_1.save()  # no field changes, but still a real save -> bumps version to 1

    assert copy_2.version == 0
    with pytest.raises(StaleObjectError):
        await copy_2.save()
    assert copy_2.version == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failed_save_rolls_back_in_memory_version_not_just_stale_case(db):
    """execute_update() bumped the in-memory version BEFORE calling execute() - the
    round-2 fix only rolled that bump back when execute() succeeded but affected 0 rows
    (a genuine concurrent-writer conflict). If execute() raised an exception outright
    instead (e.g. a UNIQUE violation on some OTHER field being updated alongside the versioned
    one), the bump was never rolled back - the in-memory version diverged from the DB's real
    value, and the next, otherwise legitimate save() spuriously raised StaleObjectError purely
    from this save's own earlier failure, not from any real concurrent writer."""
    await VersionedUnique.objects.create(name="A", tag="tag-a")
    b = await VersionedUnique.objects.create(name="B", tag="tag-b")

    b.tag = "tag-a"  # collides with a's unique tag
    with pytest.raises(IntegrityError):
        # A savepoint, not the bare call - on Postgres, a raised error poisons the whole
        # enclosing transaction (further queries fail with "current transaction is aborted"
        # until it's rolled back) - the savepoint contains that to just this one failed save,
        # matching how a real caller would recover before continuing to use the connection.
        async with Transactions.atomic():
            await b.save(update_fields=["tag"])

    assert b.version == 0
    b_from_db = await VersionedUnique.objects.get(pk=b.pk)
    assert b_from_db.version == 0

    b.tag = "tag-b-fixed"
    await b.save(update_fields=["tag"])  # must not raise StaleObjectError
    assert b.version == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_failed_save_rolls_back_auto_now_field_not_just_version(db):
    """to_db_value() mutates auto_now fields in-memory as a side effect, exactly like the
    optimistic_lock_field bump - a save() that fails outright (not just the 0-rows-affected stale case)
    must not leave an auto_now field showing a "just saved" value while the DB row's own column
    never actually changed. Confirmed as a live bug: only optimistic_lock_field was rolled back here,
    updated_at kept its post-mutation value even though the write never took effect."""
    await VersionedUniqueAutoNow.objects.create(name="A", tag="tag-a")
    b = await VersionedUniqueAutoNow.objects.create(name="B", tag="tag-b")
    original_updated_at = b.updated_at

    b.tag = "tag-a"  # collides with a's unique tag
    with pytest.raises(IntegrityError):
        async with Transactions.atomic():
            await b.save(update_fields=["tag"])

    assert b.version == 0
    assert b.updated_at == original_updated_at

    b_from_db = await VersionedUniqueAutoNow.objects.get(pk=b.pk)
    assert b_from_db.version == 0
    assert b_from_db.updated_at == original_updated_at
    assert b.updated_at == b_from_db.updated_at


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_savepoint_rollback_for_an_unrelated_reason_rolls_back_in_memory_version_too(db):
    """The two rollback paths test_failed_save_rolls_back_in_memory_version_not_just_stale_case/
    test_stale_save_rolls_back_in_memory_version both cover are triggered by THIS SAME save()
    call failing (an exception from execute(), or a genuine 0-rows stale conflict) - a
    save() that itself SUCCEEDS (a real row match, no exception) has nothing to roll back at
    that point. But if the enclosing transaction/savepoint later rolls back anyway, for a
    completely unrelated reason raised AFTER the save() already returned, the DB row correctly
    reverts via the real SQL ROLLBACK - yet nothing previously undid the in-memory version bump,
    permanently desyncing this instance from the database it was just read from. Confirmed live:
    the next, otherwise legitimate save() on the same instance spuriously raised
    StaleObjectError, as if a concurrent writer had touched the row, when none ever did."""

    class BoomError(Exception):
        pass

    thing = await VersionedThing.objects.create(name="A")
    assert thing.version == 0

    with pytest.raises(BoomError):
        async with Transactions.atomic():
            thing.name = "B"
            await thing.save()
            raise BoomError("unrelated failure, after save() already succeeded")

    assert thing.version == 0
    fresh = await VersionedThing.objects.get(pk=thing.pk)
    assert fresh.name == "A"
    assert fresh.version == 0

    thing.name = "B again"
    await thing.save()  # must not raise StaleObjectError
    assert thing.version == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_nested_savepoint_rollback_only_restores_its_own_layer(db):
    """The `db` fixture itself already wraps every test in its own outer transaction, so a save()
    made OUTSIDE any additional explicit Transactions.atomic() block still runs inside a
    savepoint the moment a NESTED one is opened. _register_rollback_restore() used to share ONE
    flat pending-restore dict per instance regardless of savepoint nesting - a field already
    registered by an EARLIER write (from the enclosing scope) claimed the "first call wins" slot
    for good, so a LATER write's own bump, made from inside a nested savepoint that alone rolls
    back, was never restored: the outer write's own registered rollback callback belongs to the
    OUTER scope, and never fires just because the inner savepoint rolled back. Confirmed live as
    a real bug (not just a hypothetical) before this fix, using exactly this shape."""

    class BoomError(Exception):
        pass

    thing = await VersionedThing.objects.create(name="A")
    thing.name = "B"
    await thing.save()
    assert thing.version == 1

    with pytest.raises(BoomError):
        async with Transactions.atomic():
            thing.name = "C"
            await thing.save()
            assert thing.version == 2
            raise BoomError("only this nested savepoint should roll back")

    # The nested savepoint's own bump (1 -> 2) is undone; the enclosing scope's earlier,
    # still-uncommitted bump (0 -> 1) is untouched - it belongs to a still-open outer scope.
    assert thing.version == 1


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_multiple_saves_in_the_same_rolled_back_scope_restore_to_the_pre_scope_version(db):
    """Two saves to the SAME instance inside the SAME still-open transaction/savepoint before it
    rolls back - the restored version must be the one from BEFORE EITHER save, not an
    intermediate value between them (each save's own optimistic bump captures a different
    "old version", so naively restoring on every save independently would apply whichever
    restore happened to run last, not necessarily the correct, earliest one)."""

    class BoomError(Exception):
        pass

    thing = await VersionedThing.objects.create(name="A")

    with pytest.raises(BoomError):
        async with Transactions.atomic():
            thing.name = "B"
            await thing.save()
            assert thing.version == 1
            thing.name = "C"
            await thing.save()
            assert thing.version == 2
            raise BoomError("unrelated failure")

    assert thing.version == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_committed_save_does_not_leave_a_stale_pending_rollback_restore(file_db):
    """A save() inside a transaction that genuinely COMMITS all the way out must not leave the
    pending-rollback-restore bookkeeping behind - a LATER, independent transaction writing to
    the same instance must still register (and correctly fire) its own restore if THAT one
    rolls back, unaffected by the earlier, already-committed write."""

    class BoomError(Exception):
        pass

    thing = await VersionedThing.objects.create(name="A")

    async with Transactions.atomic():
        thing.name = "B"
        await thing.save()
    assert thing.version == 1

    with pytest.raises(BoomError):
        async with Transactions.atomic():
            thing.name = "C"
            await thing.save()
            assert thing.version == 2
            raise BoomError("unrelated failure")

    assert thing.version == 1


@pytest.mark.asyncio
async def test_save_with_explicit_update_fields_still_checks_version(db):
    original = await VersionedThing.objects.create(name="A")
    copy_1 = await VersionedThing.objects.get(pk=original.pk)
    copy_2 = await VersionedThing.objects.get(pk=original.pk)

    copy_1.name = "From copy 1"
    await copy_1.save(update_fields=["name"])

    copy_2.name = "From copy 2"
    with pytest.raises(StaleObjectError):
        await copy_2.save(update_fields=["name"])


@pytest.mark.asyncio
async def test_save_with_version_in_explicit_update_fields_no_double_bump(db):
    """Explicitly listing the version field in update_fields must not cause it to be bumped
    twice (once for being in update_fields, once for the automatic optimistic-lock bump)."""
    thing = await VersionedThing.objects.create(name="A")
    thing.name = "B"
    await thing.save(update_fields=["name", "version"])
    assert thing.version == 1


@pytest.mark.asyncio
async def test_force_update_stale_raises_stale_object_error(db):
    original = await VersionedThing.objects.create(name="A")
    copy_1 = await VersionedThing.objects.get(pk=original.pk)
    copy_2 = await VersionedThing.objects.get(pk=original.pk)

    await copy_1.save()

    with pytest.raises(StaleObjectError):
        await copy_2.save(force_update=True)


@pytest.mark.asyncio
async def test_delete_unaffected_by_optimistic_lock_field(db):
    thing = await VersionedThing.objects.create(name="A")
    await thing.delete()
    assert not await VersionedThing.objects.filter(pk=thing.pk).exists()


@pytest.mark.asyncio
async def test_unversioned_model_unaffected(db):
    """A model with no Meta.optimistic_lock_field must behave exactly as before - no WHERE-clause
    version check, no bump, no StaleObjectError possible."""
    obj = await IntFields.objects.create(intnum=1)
    obj.intnum = 2
    await obj.save()
    refreshed = await IntFields.objects.get(pk=obj.pk)
    assert refreshed.intnum == 2


# ============================================================================
# QuerySet.update() / bulk_update() - previously bypassed Meta.optimistic_lock_field entirely (no check,
# no bump), silently defeating optimistic locking for both bulk paths. Fixed to auto-bump the
# version on every write; bulk_update() additionally checks it per-row since it's given real
# instances (each carrying the version it was read at), unlike .update() which only has a filter.
# ============================================================================


@pytest.mark.asyncio
async def test_queryset_update_bumps_version(db):
    thing = await VersionedThing.objects.create(name="A")

    rows = await VersionedThing.objects.filter(pk=thing.pk).update(name="B")

    assert rows == 1
    refreshed = await VersionedThing.objects.get(pk=thing.pk)
    assert refreshed.name == "B"
    assert refreshed.version == 1


@pytest.mark.asyncio
async def test_queryset_update_rejects_direct_version_kwarg(db):
    thing = await VersionedThing.objects.create(name="A")

    with pytest.raises(QueryError):
        await VersionedThing.objects.filter(pk=thing.pk).update(version=99)


@pytest.mark.asyncio
async def test_queryset_update_unversioned_model_unaffected(db):
    obj = await IntFields.objects.create(intnum=1)

    rows = await IntFields.objects.filter(pk=obj.pk).update(intnum=2)

    assert rows == 1
    assert (await IntFields.objects.get(pk=obj.pk)).intnum == 2


@pytest.mark.asyncio
async def test_bulk_update_bumps_version(db):
    a = await VersionedThing.objects.create(name="A")
    b = await VersionedThing.objects.create(name="B")
    a.name = "A2"
    b.name = "B2"

    rows = await VersionedThing.objects.bulk_update([a, b], fields=["name"])

    assert rows == 2
    assert a.version == 1
    assert b.version == 1
    refreshed_a = await VersionedThing.objects.get(pk=a.pk)
    refreshed_b = await VersionedThing.objects.get(pk=b.pk)
    assert (refreshed_a.name, refreshed_a.version) == ("A2", 1)
    assert (refreshed_b.name, refreshed_b.version) == ("B2", 1)


@pytest.mark.asyncio
async def test_bulk_update_stale_version_raises(db):
    thing = await VersionedThing.objects.create(name="A")
    stale_copy = await VersionedThing.objects.get(pk=thing.pk)

    thing.name = "first writer"
    await thing.save()

    stale_copy.name = "stale writer"
    with pytest.raises(StaleObjectError) as excinfo:
        await VersionedThing.objects.bulk_update([stale_copy], fields=["name"])

    # the first writer's change is untouched by the failed stale attempt
    assert (await VersionedThing.objects.get(pk=thing.pk)).name == "first writer"

    # bulk_update()'s batch error can't pinpoint a single object - model is still attached,
    # pk/expected_version are honestly None rather than guessed.
    error = excinfo.value
    assert error.model is VersionedThing
    assert error.pk is None
    assert error.expected_version is None


@pytest.mark.asyncio
async def test_bulk_update_does_not_bump_the_stale_objects_own_in_memory_version(db):
    """The real bug: every object in a batch used to get its in-memory version bumped
    unconditionally, INCLUDING the ones RETURNING never reported as actually touched - a
    genuinely-stale object's in-memory version then happened to coincide with the real (bumped by
    the concurrent writer) DB version, so its own NEXT .save() silently passed the WHERE-version
    check and clobbered the concurrent writer's change with no error at all, completely
    defeating the StaleObjectError this whole check exists to raise. Only the object(s) RETURNING
    actually reports as updated may have their in-memory version bumped."""
    a = await VersionedThing.objects.create(name="A")
    b = await VersionedThing.objects.create(name="B")
    a_stale = await VersionedThing.objects.get(pk=a.pk)
    b_stale = await VersionedThing.objects.get(pk=b.pk)

    b.name = "B-changed-by-concurrent-writer"
    await b.save()  # bumps the real DB row's version to 1

    a_stale.name = "A-bulkupdated"
    b_stale.name = "B-bulkupdated-STALE"
    with pytest.raises(StaleObjectError):
        await VersionedThing.objects.bulk_update([a_stale, b_stale], fields=["name"])

    assert a_stale.version == 1, "a_stale genuinely succeeded, its in-memory version must bump"
    assert b_stale.version == 0, "b_stale was stale - its in-memory version must NOT bump"

    # The real regression: b_stale.save() must still correctly detect the conflict, not silently
    # succeed and overwrite the concurrent writer's change.
    b_stale.name = "B-bulkupdated-STALE-retry"
    with pytest.raises(StaleObjectError):
        await b_stale.save(update_fields=["name"])
    assert (await VersionedThing.objects.get(pk=b.pk)).name == "B-changed-by-concurrent-writer"


@pytest.mark.asyncio
async def test_bulk_update_mixed_batch_one_stale_raises_for_whole_batch(db):
    """bulk_update() can't pinpoint which object in a batch was stale from a single
    affected-rowcount - the whole batch's statement fails, same all-or-nothing signal save() gives
    on a single row, just applied to the statement as a unit."""
    fresh = await VersionedThing.objects.create(name="fresh")
    stale_source = await VersionedThing.objects.create(name="stale")
    stale_copy = await VersionedThing.objects.get(pk=stale_source.pk)
    stale_source.name = "changed elsewhere"
    await stale_source.save()

    fresh.name = "fresh-updated"
    stale_copy.name = "stale-updated"
    with pytest.raises(StaleObjectError):
        await VersionedThing.objects.bulk_update([fresh, stale_copy], fields=["name"])

    # `fresh` genuinely got written before the batch as a whole was found stale - its in-memory
    # version must still be bumped to match, or a later save() on it would spuriously raise
    # StaleObjectError too (its in-memory version would be one behind the real DB row).
    assert fresh.version == 1
    refreshed_fresh = await VersionedThing.objects.get(pk=fresh.pk)
    assert (refreshed_fresh.name, refreshed_fresh.version) == ("fresh-updated", 1)
    fresh.name = "fresh-updated-again"
    await fresh.save()


@pytest.mark.asyncio
async def test_bulk_update_stale_batch_does_not_abandon_later_chunks(db):
    """batch_size splits one bulk_update() call into several independent UPDATE statements
    (chunks), each its own all-or-nothing unit - a stale object in one chunk must not affect any
    OTHER chunk's own eligibility to apply. The stale chunk's raise used to happen INSIDE the
    per-chunk loop, aborting it immediately - every chunk still left unprocessed at that point
    (i.e. every chunk ordered AFTER the stale one) was silently never even attempted, dropping
    otherwise perfectly valid updates with no error at all pointing at them."""
    objs = [await VersionedThing.objects.create(name=letter) for letter in "ABCDEF"]
    a, b, c, d, e, f = objs

    stale_copy = await VersionedThing.objects.get(pk=c.pk)
    stale_copy.name = "changed elsewhere"
    await stale_copy.save()

    for index, obj in enumerate(objs, start=1):
        obj.name = f"new-{index}"

    with pytest.raises(StaleObjectError):
        # batch_size=2 -> chunks [a, b], [c, d], [e, f] - c's chunk is stale, e/f's chunk comes
        # strictly AFTER it.
        await VersionedThing.objects.bulk_update(objs, fields=["name"], batch_size=2)

    refreshed = {obj.pk: (await VersionedThing.objects.get(pk=obj.pk)).name for obj in objs}
    assert refreshed[a.pk] == "new-1"
    assert refreshed[b.pk] == "new-2"
    assert refreshed[c.pk] == "changed elsewhere"
    assert refreshed[d.pk] == "new-4"
    assert refreshed[e.pk] == "new-5"
    assert refreshed[f.pk] == "new-6"


@pytest.mark.asyncio
async def test_bulk_update_rolls_back_auto_now_for_a_stale_object(db):
    """serialize_instances()'s to_db_value() call mutates an auto_now field to "now" in-memory
    for EVERY object before the UPDATE even runs - a genuinely-stale object (RETURNING never
    reports it as touched) must have that mutation rolled back, mirroring save()'s own
    old_auto_now_values rollback (see VersionedUniqueAutoNow's own docstring), or it would
    silently show a fresh timestamp that was never actually persisted."""
    a = await VersionedUniqueAutoNow.objects.create(name="A", tag="a")
    b = await VersionedUniqueAutoNow.objects.create(name="B", tag="b")
    a_stale = await VersionedUniqueAutoNow.objects.get(pk=a.pk)
    b_stale = await VersionedUniqueAutoNow.objects.get(pk=b.pk)
    b_stale_original_updated_at = b_stale.updated_at

    b.name = "B-changed-by-concurrent-writer"
    await b.save()  # bumps the real DB row's version to 1

    a_stale.name = "A-bulkupdated"
    b_stale.name = "B-bulkupdated-STALE"
    with pytest.raises(StaleObjectError):
        await VersionedUniqueAutoNow.objects.bulk_update([a_stale, b_stale], fields=["name"])

    assert b_stale.updated_at == b_stale_original_updated_at, (
        "b_stale was stale - its in-memory auto_now mutation must be rolled back, not left "
        "showing a fresh timestamp that was never actually written"
    )


@pytest.mark.asyncio
async def test_bulk_update_rejects_optimistic_lock_field_in_fields(db):
    thing = await VersionedThing.objects.create(name="A")

    with pytest.raises(QueryError):
        await VersionedThing.objects.bulk_update([thing], fields=["version"])


@pytest.mark.asyncio
async def test_bulk_update_unversioned_model_unaffected(db):
    a = await IntFields.objects.create(intnum=1)
    b = await IntFields.objects.create(intnum=2)
    a.intnum = 10
    b.intnum = 20

    rows = await IntFields.objects.bulk_update([a, b], fields=["intnum"])

    assert rows == 2
    assert (await IntFields.objects.get(pk=a.pk)).intnum == 10
    assert (await IntFields.objects.get(pk=b.pk)).intnum == 20


# ============================================================================
# bulk_create(on_conflict=..., update_fields=...) upsert - optimistic_lock_field bump
# ============================================================================


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_upsert_bumps_version_on_conflict(db):
    """The real bug: BulkCreateQuery._apply_on_conflict() built its ON CONFLICT DO UPDATE SET
    clause purely from the caller's own update_fields, with no equivalent to bulk_update()'s
    automatic optimistic_lock_field bump - an upsert that genuinely fired its DO UPDATE branch (matching
    an existing row) left that row's version completely unchanged, defeating optimistic locking
    for any code that reads the row again afterward expecting version to reflect the write that
    just happened."""
    existing = await VersionedUnique.objects.create(name="Alice", tag="alice-tag")
    assert existing.version == 0

    await VersionedUnique.objects.bulk_create(
        [VersionedUnique(name="Alice Updated", tag="alice-tag")],
        on_conflict=["tag"],
        update_fields=["name"],
    )

    refreshed = await VersionedUnique.objects.get(pk=existing.pk)
    assert (refreshed.name, refreshed.version) == ("Alice Updated", 1)


@pytest.mark.asyncio
async def test_bulk_create_upsert_rejects_optimistic_lock_field_in_update_fields(db):
    await VersionedUnique.objects.create(name="Alice", tag="alice-tag")

    with pytest.raises(QueryError):
        await VersionedUnique.objects.bulk_create(
            [VersionedUnique(name="Alice Updated", tag="alice-tag", version=999)],
            on_conflict=["tag"],
            update_fields=["version"],
        )


# ============================================================================
# update_or_create() - optimistic_lock_field bump + guard against the caller setting it via defaults
# ============================================================================


@pytest.mark.asyncio
async def test_update_or_create_update_branch_bumps_version(db):
    thing = await VersionedThing.objects.create(name="A")

    updated, created = await VersionedThing.objects.update_or_create(defaults={"name": "B"}, id=thing.pk)

    assert created is False
    assert (updated.name, updated.version) == ("B", 1)
    refreshed = await VersionedThing.objects.get(pk=thing.pk)
    assert (refreshed.name, refreshed.version) == ("B", 1)


@pytest.mark.asyncio
async def test_update_or_create_rejects_optimistic_lock_field_in_defaults_on_update_branch(db):
    """The real bug: update_or_create()'s update branch had no equivalent to .update()/
    bulk_update()/bulk_create()'s guard against the caller setting optimistic_lock_field directly - it
    just fed defaults straight into save(), which either silently "succeeded" only because the
    caller's guessed value happened to match the real DB version, or raised a StaleObjectError
    that misleadingly blamed a concurrent modification that never actually happened."""
    thing = await VersionedThing.objects.create(name="A")

    with pytest.raises(QueryError):
        await VersionedThing.objects.update_or_create(defaults={"name": "B", "version": 999}, id=thing.pk)

    # Neither field was touched - the guard runs before save() ever executes.
    refreshed = await VersionedThing.objects.get(pk=thing.pk)
    assert (refreshed.name, refreshed.version) == ("A", 0)


@pytest.mark.asyncio
async def test_update_or_create_allows_optimistic_lock_field_in_defaults_on_create_branch(db):
    """The CREATE fallback is unaffected by the guard above - setting an initial optimistic_lock_field on
    a brand-new row is normal Model.objects.create() behavior, not the automatic-bump machinery the guard
    protects."""
    created_obj, created = await VersionedThing.objects.update_or_create(defaults={"version": 42}, name="brand-new")

    assert created is True
    assert created_obj.version == 42


# ============================================================================
# Meta.optimistic_lock_field validation
# ============================================================================


def test_optimistic_lock_field_must_exist(db):
    meta = VersionedThing._meta
    original = meta.optimistic_lock_field
    meta.optimistic_lock_field = "not_a_real_field"
    try:
        with pytest.raises(ConfigurationError):
            ModelDefinitionChecks.validate_optimistic_lock_field(meta)
    finally:
        meta.optimistic_lock_field = original


def test_optimistic_lock_field_must_be_non_nullable_intfield(db):
    meta = VersionedThing._meta
    original = meta.optimistic_lock_field
    meta.optimistic_lock_field = "name"  # a real field, but a TextField
    try:
        with pytest.raises(ConfigurationError):
            ModelDefinitionChecks.validate_optimistic_lock_field(meta)
    finally:
        meta.optimistic_lock_field = original


# ============================================================================
# Further combinations: composite PK, track_dirty_fields, Transactions.autonomous()
#
# .only()/.defer() combinations are covered in test_only.py/test_defer.py;
# soft_delete_field + optimistic_lock_field is covered in test_soft_delete.py.
# ============================================================================


@pytest.mark.asyncio
async def test_optimistic_lock_field_with_composite_pk(db):
    obj = await VersionedComposite.objects.create(a=1, b=2, name="X")
    assert obj.version == 0

    obj.name = "X2"
    await obj.save()
    assert obj.version == 1

    refreshed = await VersionedComposite.objects.get(a=1, b=2)
    assert refreshed.name == "X2"
    assert refreshed.version == 1


@pytest.mark.asyncio
async def test_optimistic_lock_field_with_composite_pk_stale_save_raises(db):
    obj = await VersionedComposite.objects.create(a=1, b=2, name="X")
    stale_copy = await VersionedComposite.objects.get(a=1, b=2)

    obj.name = "first writer"
    await obj.save()

    stale_copy.name = "stale writer"
    with pytest.raises(StaleObjectError):
        await stale_copy.save()

    assert (await VersionedComposite.objects.get(a=1, b=2)).name == "first writer"


@pytest.mark.asyncio
async def test_optimistic_lock_field_bump_does_not_leak_into_dirty_tracking(db):
    obj = await VersionedDirtyTracked.objects.create(name="A")
    obj.name = "B"
    assert obj.get_dirty_fields() == {"name": ("A", "B")}

    await obj.save()

    assert obj.get_dirty_fields() == {}
    assert obj.version == 1


@pytest_asyncio.fixture
async def file_db(tmp_path):
    """A real, persistent connection - Transactions.autonomous() opens a genuinely separate
    connection, and a fresh connection to `:memory:` sqlite is a completely separate, empty
    database. Mirrors the ambient HARE_TEST_DB when it's already a real (Postgres) URL, so the
    Postgres-only test below actually gets a Postgres "models" connection for requires_features to
    see - falls back to a real file-backed SQLite DB otherwise."""
    from hare.contrib.test.isolated_contexts import hare_test_context

    db_url = os.getenv("HARE_TEST_DB")
    if not db_url or DatabaseUnderTest.is_file_database(db_url):
        scheme = (db_url or DatabaseUnderTest.DEFAULT_URL).split("://", 1)[0]
        db_path = tmp_path / "optimistic_lock_field_autonomous_test.sqlite"
        db_url = f"{scheme}:///{db_path}?synchronous=OFF"
    async with hare_test_context(["tests.testmodels"], db_url=db_url, connection_label="models") as ctx:
        yield ctx


@hare_test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_optimistic_lock_field_concurrent_saves_via_autonomous_one_wins(file_db):
    """Two autonomous() connections racing to save the same row - real, independent connections,
    not just two in-memory instances sharing one connection's transaction state. Postgres-only:
    SQLite serializes writers hard enough (even across independent connections to the same file)
    that the two tasks never actually overlap - both "races" resolve sequentially and succeed,
    never reproducing the actual race this test exists to catch. Confirmed empirically.

    A barrier forces both tasks to finish reading the row (and so both genuinely bind the same
    pre-image version in their own UPDATE's WHERE clause) before either is allowed to save -
    without it, asyncio.gather() offers no guarantee the two ever actually overlap: one task's
    read+write+commit could legitimately complete in full before the other's read even starts,
    in which case the second task would correctly see the already-bumped version and save
    successfully too - a real success, not a lost update, that this test would have wrongly
    flagged as a broken assertion rather than a broken guarantee about scheduling. Confirmed:
    without the barrier, this reproduced as a genuine (rare) flake under a loaded CI runner.
    """
    obj = await VersionedThing.objects.create(name="A")
    barrier = asyncio.Barrier(2)

    async def bump(name: str) -> None:
        async with Transactions.autonomous() as conn:
            fresh = await VersionedThing.objects.get(pk=obj.pk).using(conn)
            await barrier.wait()
            fresh.name = name
            await fresh.save(using=conn)

    results = await asyncio.gather(bump("B"), bump("C"), return_exceptions=True)
    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], StaleObjectError)

    refreshed = await VersionedThing.objects.get(pk=obj.pk)
    assert refreshed.version == 1


@pytest.mark.asyncio
async def test_save_without_optimistic_lock_field_raises_on_zero_rows_affected(db):
    """save() on a model with no Meta.optimistic_lock_field must still detect and raise when its UPDATE
    affects zero rows (e.g. the row was deleted concurrently) - mirroring what force_update=True
    already does unconditionally. Before this fix, the optimistic_lock_field-gated check meant a model
    without one would silently treat a no-op UPDATE as a successful save, leaving
    _saved_in_db=True and (if track_dirty_fields) re-baselining every field as clean despite
    nothing having actually been written."""
    obj = await IntFields.objects.create(intnum=1)
    await IntFields.objects.filter(pk=obj.pk).delete()

    obj.intnum = 2
    with pytest.raises(IntegrityError, match="Can't update object that doesn't exist"):
        await obj.save()


@pytest.mark.asyncio
async def test_filtered_bulk_update_does_not_report_a_filtered_out_object_as_stale(db):
    """An object whose row the queryset's own filter excludes isn't updated - it used to be
    counted as a stale version, raising StaleObjectError for a row nobody else touched."""
    kept = await VersionedThing.objects.create(name="keep")
    matched = await VersionedThing.objects.create(name="match")
    kept.name = "keep!"
    matched.name = "match!"

    rows = await VersionedThing.objects.filter(name="match").bulk_update([kept, matched], fields=["name"])

    assert rows == 1
    assert (kept.version, matched.version) == (0, 1)
    assert await VersionedThing.objects.filter(pk__in=[kept.pk, matched.pk]).order_by("id").values_list(
        "name", "version"
    ) == [
        ("keep", 0),
        ("match!", 1),
    ]


@pytest.mark.asyncio
async def test_filtered_bulk_update_still_reports_a_stale_object_the_filter_matches(db):
    """A row the filter matches but whose version moved on is still stale."""
    thing = await VersionedThing.objects.create(name="match")
    stale_copy = await VersionedThing.objects.get(pk=thing.pk)
    await thing.save()
    stale_copy.name = "stale writer"

    with pytest.raises(StaleObjectError, match="1 object"):
        await VersionedThing.objects.filter(name="match").bulk_update([stale_copy], fields=["name"])


@pytest.mark.asyncio
async def test_filtered_bulk_update_reports_a_deleted_row_as_stale(db):
    thing = await VersionedThing.objects.create(name="match")
    await VersionedThing.objects.filter(pk=thing.pk).delete()
    thing.name = "gone"

    with pytest.raises(StaleObjectError, match="1 object"):
        await VersionedThing.objects.filter(name="match").bulk_update([thing], fields=["name"])
