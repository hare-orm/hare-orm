"""A failed refresh restores the previous object without transaction callbacks."""

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.models import Model
from hare.models.instances.instance_refresh import InstanceRefresh
from hare.models.write.instance_snapshot import InstanceSnapshot

pytestmark = pytest.mark.sqlite_only


class Shelf(Model):
    id = fields.IntField(primary_key=True)


class Entry(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=30)
    amount = fields.IntField()
    shelf = fields.ForeignKeyField("models.Shelf", related_name="entries", null=True)

    class Meta:
        track_dirty_fields = True

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "amount" and value == 9 and getattr(self, "reject_refresh", False):
            raise ValueError("refresh setter failed")


@pytest.mark.parametrize("selected_fields", [None, ["shelf", "label", "amount"]])
@pytest.mark.asyncio
async def test_refresh_setter_failure_restores_fields_baseline_and_loaded_parent(selected_fields):
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        first = await Shelf.objects.create()
        second = await Shelf.objects.create()
        obj = await Entry.objects.create(label="before", amount=2, shelf=first)
        assert await obj.shelf is first
        before = InstanceSnapshot.capture_persistence(obj).values
        await Entry.objects.filter(pk=obj.pk).update(label="after", amount=9, shelf=second)
        obj.reject_refresh = True
        with pytest.raises(ValueError, match="refresh setter failed"):
            await obj.refresh_from_db(fields=selected_fields)
        assert (obj.label, obj.amount, obj.shelf_id) == ("before", 2, first.pk)
        assert await obj.shelf is first
        assert InstanceSnapshot.capture_persistence(obj).values == before
        assert not obj._pending_rollback_restore_stack
        obj.reject_refresh = False
        await obj.refresh_from_db(fields=selected_fields)
        assert (obj.label, obj.amount, obj.shelf_id) == ("after", 9, second.pk)


@pytest.mark.asyncio
async def test_refresh_reads_all_selected_values_before_assignment():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        obj = await Entry.objects.create(label="before", amount=2)
        incomplete = Entry.construct(id=obj.pk, label="after", amount=9)
        del incomplete.__dict__["amount"]
        with pytest.raises(AttributeError):
            InstanceRefresh.apply(obj, incomplete, ["label", "amount"])
        assert (obj.label, obj.amount) == ("before", 2)
