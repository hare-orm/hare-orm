import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.exceptions import (
    QueryError,
)
from hare.fields.base import DatabaseDefault
from hare.models.snapshot import FieldSnapshot
from hare.query.expressions import F
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    DirtyTrackedChild,
    DirtyTrackedComposite,
    DirtyTrackedDbDefault,
    DirtyTrackedParent,
    DirtyTrackedThing,
    DirtyTrackedUnique,
    IntFields,
    SoftDeleteVersionedDirtyTracked,
)


@pytest.mark.asyncio
async def test_new_instance_is_fully_dirty(db):
    """A never-saved instance has no prior state to diff against - every field is dirty."""
    thing = DirtyTrackedThing(name="A", count=0)
    dirty = thing.get_dirty_fields()

    assert "name" in dirty
    assert dirty["name"] == (None, "A")
    assert "count" in dirty
    assert dirty["count"] == (None, 0)


@pytest.mark.asyncio
async def test_freshly_created_instance_is_clean(db):
    """After create() (an insert + snapshot refresh), nothing is dirty anymore."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    assert thing.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_freshly_loaded_instance_is_clean(db):
    created = await DirtyTrackedThing.objects.create(name="A", count=0)
    loaded = await DirtyTrackedThing.objects.get(pk=created.pk)
    assert loaded.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_changing_a_field_marks_it_dirty(db):
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    thing.name = "B"

    dirty = thing.get_dirty_fields()
    assert dirty == {"name": ("A", "B")}


@pytest.mark.asyncio
async def test_inplace_json_mutation_is_detected(db):
    """_snapshot_dirty_fields() used to store the live JSONField dict/list object directly, not
    a copy - an in-place mutation (thing.data["x"] = 2, not thing.data = {...}) left the snapshot
    and the current value pointing at the exact same, already-mutated object, so `old != current`
    always saw them as equal and the change was invisible to get_dirty_fields()."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0, data={"x": 1})
    thing.data["x"] = 2

    dirty = thing.get_dirty_fields()
    assert dirty == {"data": ({"x": 1}, {"x": 2})}


@pytest.mark.asyncio
async def test_changing_multiple_fields(db):
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    thing.name = "B"
    thing.count = 5

    dirty = thing.get_dirty_fields()
    assert dirty == {"name": ("A", "B"), "count": (0, 5)}


@pytest.mark.asyncio
async def test_setting_field_back_to_original_value_is_not_dirty(db):
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    thing.name = "B"
    thing.name = "A"

    assert thing.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_save_resets_the_baseline(db):
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    thing.name = "B"
    await thing.save()

    assert thing.get_dirty_fields() == {}

    thing.name = "C"
    assert thing.get_dirty_fields() == {"name": ("B", "C")}


@pytest.mark.asyncio
async def test_partial_save_only_resets_the_fields_it_actually_wrote(db):
    """save(update_fields=[...]) must not launder any OTHER unsaved change as clean - it never
    reached the DB, so get_dirty_fields() must keep reporting it as dirty."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    thing.name = "B"
    thing.count = 5
    await thing.save(update_fields=["name"])

    assert thing.get_dirty_fields() == {"count": (0, 5)}

    reloaded = await DirtyTrackedThing.objects.get(id=thing.id)
    assert reloaded.name == "B"
    assert reloaded.count == 0


@pytest.mark.asyncio
async def test_bulk_update_only_resets_the_fields_it_actually_wrote(db):
    """bulk_update(objects, fields=[...]) must not launder any OTHER unsaved change on the same
    in-memory object as clean either - the same bug as the partial save() case above, but the
    bulk path used to call the FULL _snapshot_dirty_fields() unconditionally instead of syncing
    just the fields it actually wrote."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    thing.name = "B"
    thing.count = 5
    await DirtyTrackedThing.objects.bulk_update([thing], fields=["count"])

    assert thing.get_dirty_fields() == {"name": ("A", "B")}

    reloaded = await DirtyTrackedThing.objects.get(id=thing.id)
    assert reloaded.name == "A"
    assert reloaded.count == 5


@pytest.mark.asyncio
async def test_clone_resets_dirty_snapshot_so_every_field_is_dirty(db):
    """A clone represents a row that doesn't exist in the DB yet - get_dirty_fields()'s own
    documented contract ("every field is dirty relative to nonexistence" for a never-persisted
    instance) applies to it exactly as it does to a plain Model(**kwargs) - not a diff against
    whatever baseline the ORIGINAL happened to carry, which describes the original's own
    already-persisted row, not this not-yet-inserted one. Previously clone() left _dirty_snapshot
    as a (correctly non-shared, but still WRONG) copy of the original's own baseline, so a fresh
    clone only reported its pk and any field explicitly changed after cloning as dirty, instead
    of every field."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    clone = thing.clone(pk=999)

    assert clone._dirty_snapshot is None
    assert clone.get_dirty_fields() == {
        "id": (None, 999),
        "name": (None, "A"),
        "count": (None, 0),
        "nullable": (None, None),
        "data": (None, {}),
    }


@pytest.mark.asyncio
async def test_clone_dirty_snapshot_reset_does_not_share_state_with_the_original(db):
    """clone() is a shallow copy() - _dirty_snapshot must not stay the SAME dict object (or even
    the same "is None" state by accident) on both the clone and the original.
    _sync_dirty_snapshot_fields() (used by a partial save(update_fields=[...])) mutates a
    baseline dict IN PLACE rather than replacing it, so a partial save on the ORIGINAL after
    cloning must not affect the CLONE's own (reset-to-None) baseline - the exact hazard clone()'s
    own _await_when_save re-copy already guards against, just never applied to _dirty_snapshot."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    clone = thing.clone(pk=999)
    clone.name = "cloned-name"

    thing.count = 5
    await thing.save(update_fields=["count"])

    assert thing.get_dirty_fields() == {}
    assert clone.get_dirty_fields() == {
        "id": (None, 999),
        "name": (None, "cloned-name"),
        "count": (None, 0),
        "nullable": (None, None),
        "data": (None, {}),
    }


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_insert_rolled_back_leaves_every_field_dirty_again(db):
    """save()'s INSERT path used to unconditionally re-snapshot the dirty baseline right after a
    successful INSERT, with no rollback-restore registered for it - a transaction that rolled
    back for an unrelated reason left the in-memory instance falsely believing it was clean
    (matching the now-reverted DB row) instead of reporting every field dirty again, the correct
    state for something that (per the rollback) was never actually persisted."""

    class Boom(Exception):
        pass

    thing = DirtyTrackedThing(name="x")
    with pytest.raises(Boom):
        async with Transactions.atomic():
            await thing.save()
            assert thing.get_dirty_fields() == {}
            raise Boom()

    assert thing._dirty_snapshot is None
    assert set(thing.get_dirty_fields().keys()) == {"id", "name", "count", "nullable", "data"}

    await thing.save()
    fresh = await DirtyTrackedThing.objects.get(pk=thing.pk)
    assert fresh.name == "x"


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_update_rolled_back_restores_the_dirty_snapshot(db):
    """save()'s UPDATE path used to unconditionally re-sync the dirty baseline to the just-
    written value, with no rollback-restore registered for it either - a transaction that rolled
    back afterward left get_dirty_fields() reporting the field as clean even though the DB row's
    real value never actually changed."""

    class Boom(Exception):
        pass

    thing = await DirtyTrackedThing.objects.create(name="z", count=0)
    with pytest.raises(Boom):
        async with Transactions.atomic():
            thing.name = "z2"
            await thing.save()
            assert thing.get_dirty_fields() == {}
            raise Boom()

    assert thing.get_dirty_fields() == {"name": ("z", "z2")}
    fresh = await DirtyTrackedThing.objects.get(pk=thing.pk)
    assert fresh.name == "z"


@pytest.mark.asyncio
async def test_partial_save_of_json_field_deepcopies_the_new_snapshot_value(db):
    """_sync_dirty_snapshot_fields() used to store the live JSONField dict/list object directly
    into the snapshot after a partial save(update_fields=[...]) - a later in-place mutation
    (thing.data["x"] = 2, not thing.data = {...}) then mutated the snapshot right along with the
    live value, permanently hiding that field from get_dirty_fields() instead of reporting it
    dirty. Mirrors test_inplace_json_mutation_is_detected, but through the partial-save path."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0, data={"x": 1})
    thing.data = {"x": 2}
    await thing.save(update_fields=["data"])

    assert thing.get_dirty_fields() == {}

    thing.data["x"] = 3
    dirty = thing.get_dirty_fields()
    assert dirty == {"data": ({"x": 2}, {"x": 3})}


@pytest.mark.asyncio
async def test_nullable_field_none_to_value_is_dirty(db):
    thing = await DirtyTrackedThing.objects.create(name="A", count=0, nullable=None)
    thing.nullable = "set now"

    dirty = thing.get_dirty_fields()
    assert dirty == {"nullable": (None, "set now")}


@pytest.mark.asyncio
async def test_partial_only_query_tracks_loaded_fields(db):
    created = await DirtyTrackedThing.objects.create(name="A", count=0)
    partial = await DirtyTrackedThing.objects.get(pk=created.pk).only("name")

    assert partial.get_dirty_fields() == {}
    partial.name = "B"
    assert partial.get_dirty_fields() == {"name": ("A", "B")}


@pytest.mark.asyncio
async def test_requires_track_dirty_fields_configured(db):
    obj = await IntFields.objects.create(intnum=1)
    with pytest.raises(QueryError):
        obj.get_dirty_fields()


@pytest.mark.asyncio
async def test_relation_fields_excluded_from_dirty_tracking(db):
    """get_dirty_fields() covers direct fields only - relation attribute names (as opposed to
    their shadow *_id columns) are never included."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    dirty_field_names = set(thing.get_dirty_fields().keys())
    assert dirty_field_names.issubset(DirtyTrackedThing._meta.fields_map.keys() - DirtyTrackedThing._meta.fetch_fields)


@pytest.mark.asyncio
async def test_bulk_update_resets_dirty_snapshot(db):
    """bulk_update() doesn't go through save() at all (it builds raw SQL directly from the passed
    objects) - previously left the snapshot stale, so get_dirty_fields() kept reporting changes as
    unsaved even after a successful bulk_update() had already persisted them."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=0)
    thing.count = 5
    assert thing.get_dirty_fields() == {"count": (0, 5)}

    await DirtyTrackedThing.objects.bulk_update([thing], fields=["count"])

    assert thing.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_bulk_update_resets_snapshot_for_every_object_in_batch(db):
    a = await DirtyTrackedThing.objects.create(name="A", count=0)
    b = await DirtyTrackedThing.objects.create(name="B", count=0)
    a.count = 1
    b.count = 2

    await DirtyTrackedThing.objects.bulk_update([a, b], fields=["count"])

    assert a.get_dirty_fields() == {}
    assert b.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_bulk_update_does_not_snapshot_objects_excluded_by_the_queryset_own_filter(db):
    """QuerySet.bulk_update() (unlike the Model.objects.bulk_update() facade, which always matches purely
    by PK) can be called on an already-filtered queryset - nothing stops it, and it's a real,
    documented API. An object the filter excludes never actually gets its row written, but used
    to still get unconditionally snapshotted clean, permanently hiding its real unsaved change
    from get_dirty_fields() with no way to detect it went unwritten."""
    excluded = await DirtyTrackedThing.objects.create(name="a", count=0)
    included = await DirtyTrackedThing.objects.create(name="b", count=1)
    excluded.name = "a2"
    included.name = "b2"

    await DirtyTrackedThing.objects.filter(count__gt=0).bulk_update([excluded, included], fields=["name"])

    assert excluded.get_dirty_fields() == {"name": ("a", "a2")}, "excluded by the filter - must stay dirty"
    assert included.get_dirty_fields() == {}

    reloaded_excluded = await DirtyTrackedThing.objects.get(pk=excluded.pk)
    assert reloaded_excluded.name == "a"


@pytest.mark.asyncio
async def test_bulk_create_snapshots_dirty_fields_for_every_object_in_batch(db):
    """bulk_create() doesn't go through save() at all - previously left every object's
    dirty-tracking snapshot at its __init__-time default forever, so get_dirty_fields() kept
    reporting every field as unsaved even right after a successful bulk_create() had already
    persisted them."""
    a = DirtyTrackedThing(name="A", count=0)
    b = DirtyTrackedThing(name="B", count=1)

    await DirtyTrackedThing.objects.bulk_create([a, b])

    assert a.get_dirty_fields() == {}
    assert b.get_dirty_fields() == {}


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts_does_not_snapshot_a_skipped_object_as_clean(db):
    """ON CONFLICT DO NOTHING can silently skip inserting an object's row entirely - unlike a
    successful (non-conflicting) bulk_create(), there's no cheap, universally-available way to
    tell which objects actually got a row written (see BulkCreateQuery._run()'s own comment).
    Snapshotting a skipped object as clean anyway used to permanently launder its real,
    never-persisted data as "already saved" with no way to detect it went unwritten."""
    await DirtyTrackedUnique.objects.create(sku="A1", name="Widget")
    dup = DirtyTrackedUnique(sku="A1", name="Duplicate")

    await DirtyTrackedUnique.objects.bulk_create([dup], ignore_conflicts=True)

    assert dup.get_dirty_fields() != {}, "skipped by ON CONFLICT DO NOTHING - must not be reported clean"
    assert await DirtyTrackedUnique.objects.all().count() == 1


# ============================================================================
# Further combinations: select_related/prefetch_related independence, composite PK,
# Transactions.autonomous()
# ============================================================================


@pytest.mark.asyncio
async def test_select_related_object_has_independent_dirty_snapshot(db):
    parent = await DirtyTrackedParent.objects.create(name="P")
    child = await DirtyTrackedChild.objects.create(name="C", parent=parent)

    fetched = await DirtyTrackedChild.objects.filter(pk=child.pk).select_related("parent").first()
    assert fetched.get_dirty_fields() == {}
    assert fetched.parent.get_dirty_fields() == {}

    fetched.parent.name = "Changed"
    assert fetched.parent.get_dirty_fields() == {"name": ("P", "Changed")}
    assert fetched.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_prefetch_related_object_has_independent_dirty_snapshot(db):
    parent = await DirtyTrackedParent.objects.create(name="P")
    child = await DirtyTrackedChild.objects.create(name="C", parent=parent)

    fetched = await DirtyTrackedChild.objects.filter(pk=child.pk).prefetch_related("parent").first()
    fetched.parent.name = "Changed"

    assert fetched.parent.get_dirty_fields() == {"name": ("P", "Changed")}
    assert fetched.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_dirty_tracking_with_composite_pk(db):
    obj = await DirtyTrackedComposite.objects.create(a=1, b=2, name="X")
    assert obj.get_dirty_fields() == {}

    obj.name = "Y"
    assert obj.get_dirty_fields() == {"name": ("X", "Y")}

    await obj.save()
    assert obj.get_dirty_fields() == {}


@pytest_asyncio.fixture
async def file_db(tmp_path):
    """A real file-backed SQLite DB - Transactions.autonomous() opens a genuinely separate
    connection, and a fresh connection to `:memory:` is a completely separate, empty database."""
    from hare.contrib.test.helpers import hare_test_context

    db_path = tmp_path / "dirty_tracking_autonomous_test.sqlite"
    async with hare_test_context(
        ["tests.testmodels"], db_url=f"sqlite:///{db_path}?synchronous=OFF", connection_label="models"
    ) as ctx:
        yield ctx


@pytest.mark.asyncio
async def test_dirty_tracking_save_via_autonomous_transaction(file_db):
    obj = await DirtyTrackedThing.objects.create(name="A", count=0)
    obj.count = 5
    assert obj.get_dirty_fields() == {"count": (0, 5)}

    async with Transactions.autonomous() as conn:
        await obj.save(using=conn)

    assert obj.get_dirty_fields() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_value", "new_value"),
    [
        ({"a": 1}, {"a": True}),
        ({"a": 1}, {"a": 1.0}),
        ({"a": [0]}, {"a": [False]}),
        ([1, {"b": 0}], [1, {"b": False}]),
        (1, True),
    ],
)
async def test_json_value_changing_only_a_nested_type_is_dirty(db, stored_value, new_value):
    created = await DirtyTrackedThing.objects.create(name="A", data=stored_value)
    thing = await DirtyTrackedThing.objects.get(pk=created.pk)

    thing.data = new_value
    dirty_fields = thing.get_dirty_fields()
    assert dirty_fields == {"data": (stored_value, new_value)}
    await thing.save(update_fields=list(dirty_fields))

    stored = await DirtyTrackedThing.objects.get(pk=created.pk)
    assert stored.data == new_value
    assert FieldSnapshot.is_same_typed_value(stored.data, new_value)
    assert thing.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_json_value_reassigned_unchanged_is_clean(db):
    created = await DirtyTrackedThing.objects.create(name="A", data={"a": 1, "b": [1.5, None, "x"]})
    thing = await DirtyTrackedThing.objects.get(pk=created.pk)

    thing.data = {"b": [1.5, None, "x"], "a": 1}
    thing.count = 0

    assert thing.get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_db_default_placeholder_is_not_dirty_against_its_own_snapshot(db):
    """FieldSnapshot deep-copied the DatabaseDefault placeholder, and the copy never compared
    equal to the original - a field left to its db_default read as changed right after a snapshot
    and after bulk_create()."""
    unsaved = DirtyTrackedDbDefault(name="a")
    assert isinstance(unsaved.counter, DatabaseDefault)
    assert unsaved.diff_against(unsaved.snapshot()) == {}

    created = [DirtyTrackedDbDefault(name="b")]
    await DirtyTrackedDbDefault.objects.bulk_create(created)
    assert created[0].get_dirty_fields() == {}


@pytest.mark.asyncio
async def test_expression_saved_on_an_instance_is_clean_until_refresh(db):
    """An F()-expression stays on the instance after save() - it used to read as dirty against a
    deep copy of itself."""
    thing = await DirtyTrackedThing.objects.create(name="A", count=1)
    thing.count = F("count") + 1
    assert set(thing.get_dirty_fields()) == {"count"}

    await thing.save()
    assert thing.get_dirty_fields() == {}

    await thing.refresh_from_db()
    assert thing.count == 2
    assert thing.get_dirty_fields() == {}


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_rolled_back_delete_and_restore_restore_the_dirty_baseline(db):
    """delete()/restore() synced the dirty baseline without registering it for a rollback - after
    the enclosing transaction rolled back, the reverted version/soft-delete values read as dirty."""
    thing = await SoftDeleteVersionedDirtyTracked.objects.create(name="A")

    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            await thing.delete()
            raise RuntimeError("roll back")
    assert thing.deleted_at is None
    assert thing.get_dirty_fields() == {}

    await thing.delete()
    with pytest.raises(RuntimeError):
        async with Transactions.atomic():
            await thing.restore()
            raise RuntimeError("roll back")
    assert thing.deleted_at is not None
    assert thing.get_dirty_fields() == {}
