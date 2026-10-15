import datetime
import uuid
from decimal import Decimal

import pytest

from hare.exceptions import FieldError, IncompleteInstanceError
from hare.models import FieldSnapshot
from hare.models.instances.dirty_fields import DirtyFields
from tests.testmodels import (
    Currency,
    DatetimeFields,
    DecimalFields,
    DirtyTrackedThing,
    EnumFields,
    JSONFields,
    Service,
    TimeDeltaFields,
    UUIDFields,
)


@pytest.mark.asyncio
async def test_in_place_json_mutation_is_visible_in_diff(db):
    record = await JSONFields.objects.create(data={"url": "a", "tags": [1]})
    snapshot = record.snapshot()

    record.data["url"] = "b"
    record.data["tags"].append(2)

    assert record.diff_against(snapshot) == {
        "data": ({"url": "a", "tags": [1]}, {"url": "b", "tags": [1, 2]}),
    }


@pytest.mark.asyncio
async def test_unchanged_instance_has_empty_diff(db):
    record = await JSONFields.objects.create(data={"url": "a"})
    assert record.diff_against(record.snapshot()) == {}


@pytest.mark.asyncio
async def test_reassignment_is_visible_in_diff(db):
    record = await DirtyTrackedThing.objects.create(name="A", count=1)
    snapshot = record.snapshot()
    record.name = "B"
    record.count = 2
    assert record.diff_against(snapshot) == {"name": ("A", "B"), "count": (1, 2)}


@pytest.mark.asyncio
async def test_default_snapshot_covers_every_direct_field(db):
    record = await JSONFields.objects.create(data={"url": "a"})
    assert record.snapshot().fields == JSONFields._meta.direct_fields


@pytest.mark.asyncio
async def test_fields_argument_limits_snapshot_and_diff(db):
    record = await DirtyTrackedThing.objects.create(name="A", count=1)
    snapshot = record.snapshot(fields=["name"])
    record.name = "B"
    record.count = 2

    assert snapshot.fields == frozenset({"name"})
    assert dict(snapshot) == {"name": "A"}
    assert record.diff_against(snapshot) == {"name": ("A", "B")}


@pytest.mark.asyncio
async def test_fields_argument_accepts_any_iterable_and_ignores_duplicates(db):
    record = await DirtyTrackedThing.objects.create(name="A", count=1)
    snapshot = record.snapshot(fields=(name for name in ["name", "count", "name"]))
    assert snapshot.fields == frozenset({"name", "count"})


@pytest.mark.asyncio
async def test_unknown_field_name_raises_field_error(db):
    record = await DirtyTrackedThing.objects.create(name="A")
    with pytest.raises(FieldError, match="no direct field 'nope'"):
        record.snapshot(fields=["name", "nope"])


@pytest.mark.asyncio
async def test_bare_string_fields_argument_raises_type_error(db):
    record = await DirtyTrackedThing.objects.create(name="A")
    with pytest.raises(TypeError, match="iterable of field names"):
        record.snapshot(fields="name")


@pytest.mark.asyncio
async def test_empty_fields_argument_gives_empty_snapshot(db):
    record = await DirtyTrackedThing.objects.create(name="A")
    snapshot = record.snapshot(fields=[])
    record.name = "B"
    assert len(snapshot) == 0
    assert record.diff_against(snapshot) == {}


@pytest.mark.asyncio
async def test_partial_instance_default_snapshot_skips_unloaded_fields(db):
    created = await DirtyTrackedThing.objects.create(name="A", count=1)
    partial = await DirtyTrackedThing.objects.get(pk=created.pk).only("id", "name")
    snapshot = partial.snapshot()
    assert snapshot.fields == frozenset({"id", "name"})
    partial.name = "B"
    assert partial.diff_against(snapshot) == {"name": ("A", "B")}


@pytest.mark.asyncio
async def test_partial_instance_explicit_unloaded_field_raises(db):
    created = await DirtyTrackedThing.objects.create(name="A", count=1)
    partial = await DirtyTrackedThing.objects.get(pk=created.pk).only("id", "name")
    with pytest.raises(IncompleteInstanceError, match="'count' is not loaded"):
        partial.snapshot(fields=["count"])


@pytest.mark.asyncio
async def test_immutable_values_are_not_copied(db):
    moment = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    datetime_record = DatetimeFields(datetime=moment)
    decimal_record = DecimalFields(decimal=Decimal("1.5"), decimal_nodec=Decimal("2"))
    uuid_record = UUIDFields(data=uuid.uuid4())
    enum_record = EnumFields(service=Service.database_design, currency=Currency.EUR)
    timedelta_record = TimeDeltaFields(timedelta=datetime.timedelta(seconds=5))
    thing = DirtyTrackedThing(name="text", count=7)

    for instance, field_names in (
        (datetime_record, ["datetime"]),
        (decimal_record, ["decimal", "decimal_nodec"]),
        (uuid_record, ["id", "data"]),
        (enum_record, ["service", "currency"]),
        (timedelta_record, ["timedelta"]),
        (thing, ["name", "count", "nullable"]),
    ):
        snapshot = instance.snapshot()
        for field_name in field_names:
            assert snapshot[field_name] is getattr(instance, field_name), field_name


def test_immutable_values_are_not_copied_by_copy_value():
    values = [
        1,
        1.5,
        "text",
        True,
        b"bytes",
        None,
        datetime.datetime.now(),
        datetime.date.today(),
        datetime.time(1, 2),
        datetime.timedelta(days=1),
        Decimal("3.14"),
        uuid.uuid4(),
        Currency.USD,
        Service.python_programming,
    ]
    for value in values:
        assert FieldSnapshot.copy_value(value) is value


def test_mutable_values_are_deep_copied():
    for value in ({"a": [1]}, [{"a": 1}], {1, 2}, bytearray(b"ab")):
        copied_value = FieldSnapshot.copy_value(value)
        assert copied_value == value
        assert copied_value is not value
    nested = {"a": [1]}
    assert FieldSnapshot.copy_value(nested)["a"] is not nested["a"]


@pytest.mark.asyncio
async def test_snapshot_is_immutable(db):
    record = await DirtyTrackedThing.objects.create(name="A")
    snapshot = record.snapshot()
    with pytest.raises(TypeError):
        snapshot["name"] = "B"  # type: ignore[index]
    with pytest.raises(AttributeError):
        snapshot.model_class = JSONFields  # type: ignore[misc]
    with pytest.raises(AttributeError):
        snapshot.new_attribute = 1  # type: ignore[attr-defined]
    assert snapshot.model_class is DirtyTrackedThing


@pytest.mark.asyncio
async def test_snapshot_of_unsaved_instance(db):
    thing = DirtyTrackedThing(name="A")
    snapshot = thing.snapshot()
    thing.name = "B"
    assert thing.diff_against(snapshot) == {"name": ("A", "B")}


@pytest.mark.asyncio
async def test_diff_against_snapshot_of_another_instance_of_same_model(db):
    first = await DirtyTrackedThing.objects.create(name="A", count=1)
    second = await DirtyTrackedThing.objects.create(name="A", count=2)
    assert second.diff_against(first.snapshot(fields=["name", "count"])) == {"count": (1, 2)}


@pytest.mark.asyncio
async def test_diff_against_snapshot_of_other_model_raises(db):
    record = await JSONFields.objects.create(data={})
    thing = await DirtyTrackedThing.objects.create(name="A")
    with pytest.raises(TypeError, match="snapshot of JSONFields"):
        thing.diff_against(record.snapshot())


@pytest.mark.asyncio
async def test_diff_against_non_snapshot_raises(db):
    thing = await DirtyTrackedThing.objects.create(name="A")
    with pytest.raises(TypeError, match="expects a FieldSnapshot"):
        thing.diff_against({"name": "B"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_snapshot_survives_save_and_refresh(db):
    record = await JSONFields.objects.create(data={"url": "a"})
    snapshot = record.snapshot()
    record.data["url"] = "b"
    await record.save()
    await record.refresh_from_db()
    assert record.diff_against(snapshot) == {"data": ({"url": "a"}, {"url": "b"})}


@pytest.mark.asyncio
async def test_untracked_model_load_and_save_take_no_snapshots(db, monkeypatch):
    capture_calls = []
    original_capture = DirtyFields.capture_field_values

    def counting_capture(self, field_names):
        capture_calls.append(type(self).__name__)
        return original_capture(self, field_names)

    monkeypatch.setattr(DirtyFields, "capture_field_values", counting_capture)

    record = await JSONFields.objects.create(data={"url": "a"})
    record.data["url"] = "b"
    await record.save()
    await record.save(update_fields=["data"])
    loaded = await JSONFields.objects.get(pk=record.pk)
    loaded_list = await JSONFields.objects.filter(pk=record.pk)
    await loaded.refresh_from_db()

    assert capture_calls == []
    for instance in (record, loaded, *loaded_list):
        assert getattr(instance, "_dirty_snapshot", None) is None

    loaded.snapshot()
    assert capture_calls == ["JSONFields"]


@pytest.mark.asyncio
async def test_tracked_model_get_dirty_fields_still_uses_its_own_baseline(db):
    thing = await DirtyTrackedThing.objects.create(name="A", data={"a": 1})
    public_snapshot = thing.snapshot(fields=["name"])
    thing.data["a"] = 2
    thing.name = "B"
    assert thing.get_dirty_fields() == {"data": ({"a": 1}, {"a": 2}), "name": ("A", "B")}
    assert thing.diff_against(public_snapshot) == {"name": ("A", "B")}
    await thing.save()
    assert thing.get_dirty_fields() == {}
    assert thing.diff_against(public_snapshot) == {"name": ("A", "B")}


@pytest.mark.asyncio
async def test_tracked_model_dirty_baseline_shares_immutable_values(db):
    thing = await DirtyTrackedThing.objects.create(name="A", count=100000, data={"when": "x"})
    assert thing._dirty_snapshot is not None
    assert thing._dirty_snapshot["name"] is thing.name
    assert thing._dirty_snapshot["count"] is thing.count
    assert thing._dirty_snapshot["data"] is not thing.data


def test_field_snapshot_repr_and_mapping_protocol():
    snapshot = FieldSnapshot(DirtyTrackedThing, {"name": "A"})
    assert "DirtyTrackedThing" in repr(snapshot)
    assert list(snapshot) == ["name"]
    assert snapshot.get("missing") is None
    assert snapshot == {"name": "A"}


@pytest.mark.asyncio
async def test_diff_against_reports_a_json_value_changing_only_a_nested_type(db):
    thing = await DirtyTrackedThing.objects.create(name="A", data={"a": 1, "b": 0})
    snapshot = thing.snapshot()

    thing.data = {"a": True, "b": 0}

    assert thing.diff_against(snapshot) == {"data": ({"a": 1, "b": 0}, {"a": True, "b": 0})}


def test_diff_against_keeps_equal_decimals_of_different_scale_clean():
    record = DecimalFields(decimal=Decimal("1.50"), decimal_nodec=Decimal("2"))
    snapshot = record.snapshot()

    record.decimal = Decimal("1.5")

    assert record.diff_against(snapshot) == {}
