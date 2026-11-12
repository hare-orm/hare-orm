"""bulk_create captures the persistence state of all its objects and publishes them saved in one pass each."""

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.models import Model
from hare.models.write.instance_snapshot import InstanceSnapshot

pytestmark = pytest.mark.sqlite_only


class Tally(Model):
    id = fields.IntField(primary_key=True)
    amount = fields.IntField()

    class Meta:
        track_dirty_fields = True


class Mark(Model):
    code = fields.CharField(max_length=10, primary_key=True)
    amount = fields.IntField()


@pytest.mark.asyncio
async def test_captured_snapshots_of_many_objects_equal_the_snapshot_of_each():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        saved = await Tally.objects.create(amount=1)
        saved.amount = 2
        fresh = Tally(amount=3)
        given_key = Tally(id=50, amount=4)
        objects = [saved, fresh, given_key]

        snapshots = InstanceSnapshot.capture_persistence_of_all(Tally, objects)

        assert [snapshot.instance for snapshot in snapshots] == objects
        assert [snapshot.values for snapshot in snapshots] == [
            InstanceSnapshot.capture_persistence(obj).values for obj in objects
        ]
        assert list(snapshots[0].values) == [
            "_saved_in_db",
            "_custom_generated_pk",
            "_connection_alias",
            "_dirty_snapshot",
            "pk",
        ]
        assert snapshots[0].values["pk"] == saved.pk
        assert snapshots[1].values["pk"] is None


@pytest.mark.asyncio
async def test_captured_snapshot_copies_the_dirty_baseline_and_marks_missing_state():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        saved = await Tally.objects.create(amount=1)
        # A new instance whose key the database generates holds no flag of its own.
        fresh = Tally(amount=3)
        assert "_custom_generated_pk" not in fresh.__dict__

        saved_snapshot, fresh_snapshot = InstanceSnapshot.capture_persistence_of_all(Tally, [saved, fresh])

        baseline = saved.__dict__["_dirty_snapshot"]
        assert saved_snapshot.values["_dirty_snapshot"] == baseline
        assert saved_snapshot.values["_dirty_snapshot"] is not baseline
        assert fresh_snapshot.values["_custom_generated_pk"] is ROLLBACK_RESTORE_UNSET


@pytest.mark.asyncio
async def test_captured_snapshot_without_primary_key_leaves_pk_out():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        (snapshot,) = InstanceSnapshot.capture_persistence_of_all(Tally, [Tally(amount=1)], include_primary_key=False)

        assert "pk" not in snapshot.values


@pytest.mark.asyncio
async def test_bulk_create_publishes_every_object_saved_keeping_given_keys_marked():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        objects = [Tally(amount=index) for index in range(5)] + [Tally(id=100, amount=9)]

        await Tally.objects.bulk_create(objects)

        assert all(obj._saved_in_db for obj in objects)
        assert {obj._connection_alias for obj in objects} == {"default"}
        assert not any(obj._custom_generated_pk for obj in objects[:-1])
        assert objects[-1]._custom_generated_pk
        assert not any(obj.get_dirty_fields() for obj in objects)
        objects[0].amount = 40
        assert set(objects[0].get_dirty_fields()) == {"amount"}


@pytest.mark.asyncio
async def test_bulk_create_ignoring_conflicts_publishes_only_the_inserted_objects():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        await Mark.objects.create(code="taken", amount=1)
        taken = Mark(code="taken", amount=2)
        free = Mark(code="free", amount=3)

        inserted = await Mark.objects.bulk_create([taken, free], ignore_conflicts=True)

        assert inserted == [free]
        assert free._saved_in_db
        assert not taken._saved_in_db
