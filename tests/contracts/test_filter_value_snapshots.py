"""Filter calls and pending writes retain container values independently of later mutations."""

import pytest
import pytest_asyncio

from hare import Model, fields
from hare.contrib.test import hare_test_context, requires_features
from hare.query.expressions import Case, Q, Value, When
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.filter_snapshot_models import CompositeSnapshotItem, SnapshotItem
from tests.contracts.snapshot_containers import JsonDocument, KeyList, KeySet
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.first_key_list import FirstKeyList
from tests.utils.first_key_set import FirstKeySet
from tests.utils.single_pass_values import SinglePassValues


@pytest_asyncio.fixture(scope="module")
async def snapshot_rows():
    async with hare_test_context(["tests.contracts.filter_snapshot_models"], db_url=DatabaseUnderTest.get_url()):
        await SnapshotItem.objects.bulk_create(
            [SnapshotItem(id=number, payload={"value": number}) for number in (1, 2)]
        )
        await CompositeSnapshotItem.objects.bulk_create(
            [CompositeSnapshotItem(group=1, number=number, payload={"value": number}) for number in (1, 2)]
        )
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize("container", (list, KeyList, set, KeySet))
async def test_membership_inputs_are_taken_at_each_filter_call(snapshot_rows, container):
    for model, first, second in ((SnapshotItem, 1, 2), (CompositeSnapshotItem, (1, 1), (1, 2))):
        for path in ("filter", "exclude", "Q"):
            keys = container([first])
            if path == "Q":
                condition = Q(pk__in=keys)
            else:
                query = getattr(model.objects, path)(pk__in=keys)
            keys.clear()
            if isinstance(keys, set):
                keys.add(second)
            else:
                keys.append(second)
            if path == "Q":
                query = model.objects.filter(condition)
            expected = second if path == "exclude" else first
            for _ in range(2):
                assert [obj.pk for obj in await query] == [expected]
                assert await query.count() == 1
                assert await query.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True], ids=["plain", "literal"])
@pytest.mark.parametrize("iterable_type", ["generator", "single-pass", "list", "dict-keys"])
async def test_literal_membership_inputs_are_captured_before_pending_queries_are_rebuilt(
    snapshot_rows, wrapped, iterable_type
):
    for model, first, second in ((SnapshotItem, 1, 2), (CompositeSnapshotItem, (1, 1), (1, 2))):
        for path in ("filter", "exclude", "Q"):
            backing = [first]
            if iterable_type == "generator":
                requested = (value for value in backing)
            elif iterable_type == "single-pass":
                requested = SinglePassValues(backing)
            elif iterable_type == "dict-keys":
                backing = dict.fromkeys(backing)
                requested = backing.keys()
            else:
                requested = backing
            value = Value(requested) if wrapped else requested
            if path == "Q":
                condition = Q(pk__in=value)
                query = model.objects.filter(condition)
            else:
                query = getattr(model.objects, path)(pk__in=value)
            if isinstance(requested, SinglePassValues):
                assert requested.iterations == 1
            backing.clear()
            expected = second if path == "exclude" else first
            for repeat in (0, 1):
                assert [obj.pk for obj in await query] == [expected]
                assert await query.count() == 1
            if isinstance(requested, SinglePassValues):
                assert requested.iterations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("container", (FirstKeyList, FirstKeySet))
async def test_membership_snapshots_preserve_custom_iteration(snapshot_rows, container):
    for model, first, second in ((SnapshotItem, 1, 2), (CompositeSnapshotItem, (1, 1), (1, 2))):
        keys = container([first, second])
        query = model.objects.filter(pk__in=keys)
        keys.clear()
        assert [obj.pk for obj in await query] == [first]


@pytest.mark.asyncio
@pytest.mark.parametrize("container", (dict, JsonDocument))
async def test_json_filter_and_write_inputs_are_deep_snapshots(snapshot_rows, container):
    for model in (SnapshotItem, CompositeSnapshotItem):
        for path in ("filter", "exclude", "Q", "update"):
            value = container(value=1)
            if path == "update":
                query = model.objects.filter(payload={"value": 2}).update(payload=value)
            elif path == "Q":
                condition = Q(payload=value)
                value["value"] = 99
                query = model.objects.filter(condition)
            else:
                query = getattr(model.objects, path)(payload=value)
            value["value"] = 99
            if path == "update":
                try:
                    await query
                    assert await model.objects.filter(payload={"value": 1}).count() == 2
                finally:
                    await model.objects.filter(pk=2 if model is SnapshotItem else (1, 2)).update(payload={"value": 2})
            else:
                assert [obj.payload for obj in await query] == [{"value": 2 if path == "exclude" else 1}]

        value = container(data={"items": [1]})
        query = model.objects.all().update(payload=value)
        value["data"]["items"].append(99)
        try:
            await query
            assert await model.objects.values_list("payload", flat=True) == [{"data": {"items": [1]}}] * 2
        finally:
            for number in (1, 2):
                await model.objects.filter(pk=number if model is SnapshotItem else (1, number)).update(
                    payload={"value": number}
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("container", (list, KeyList))
async def test_json_array_writes_copy_nested_values(snapshot_rows, container):
    for model in (SnapshotItem, CompositeSnapshotItem):
        value = container([{"items": [1]}])
        query = model.objects.all().update(payload=value)
        value[0]["items"].append(99)
        value.append({"items": [2]})
        try:
            await query
            assert await model.objects.values_list("payload", flat=True) == [[{"items": [1]}]] * 2
        finally:
            for number in (1, 2):
                await model.objects.filter(pk=number if model is SnapshotItem else (1, number)).update(
                    payload={"value": number}
                )


@pytest.mark.asyncio
async def test_nested_write_literals_and_existing_conditions_are_captured(snapshot_rows):
    for model, first, second in ((SnapshotItem, 1, 2), (CompositeSnapshotItem, (1, 1), (1, 2))):
        then_payload = {"labels": ["élan"]}
        default_payload = {"labels": ["default"]}
        keys = [first]
        expression = Case(When(pk__in=keys, then=Value(then_payload)), default=default_payload)
        # When has already constructed its condition just as a standalone Q would.
        keys[:] = [second]
        pending = model.objects.all().update(payload=expression)
        then_payload["labels"].append("later")
        default_payload["labels"].clear()
        try:
            await pending
            assert (await model.objects.get(pk=first)).payload == {"labels": ["élan"]}
            assert (await model.objects.get(pk=second)).payload == {"labels": ["default"]}
        finally:
            await model.objects.filter(pk=first).update(payload={"value": 1})
            await model.objects.filter(pk=second).update(payload={"value": 2})


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", (False, True), ids=("scalar", "composite"))
@pytest.mark.parametrize("container", (list, tuple, Value))
async def test_binary_array_writes_capture_views_before_the_buffer_changes(contract_database, composite, container):
    class Item(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField()
        blobs = fields.ArrayField(fields.BinaryField())
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    contract_database.register_live_models([Item], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    buffer = bytearray(b"abcdef")
    views = [memoryview(buffer)[::2]]
    created = await Item.objects.create(number=1, region=7, blobs=views)
    assert created.blobs == [b"ace"]
    assert await Item.objects.values_list("blobs", flat=True) == [[b"ace"]]

    buffer[:] = b"bdf000"
    update = Item.objects.filter(number=1).update(blobs=container(views)).returning("blobs")
    buffer[:] = b"zyxwvu"
    assert await update == [{"blobs": [b"bf0"]}]
    assert await Item.objects.values_list("blobs", flat=True) == [[b"bf0"]]

    features = contract_database.get_connection(CONTRACT_CONNECTION).features
    if features.supports_merge:
        buffer[:] = b"abcdef"
        on = ("region", "number") if composite else "number"
        merge = (
            Item.objects.merge([{"number": 1, "region": 7}, {"number": 2, "region": 7}], on=on)
            .when_matched(update={"blobs": container(views)})
            .when_not_matched(insert={"number": 2, "region": 7, "blobs": container(views)})
        )
        buffer[:] = b"zyxwvu"
        if features.supports_merge_returning:
            rows = await merge.returning("number", "blobs")
            assert sorted((row["number"], row["blobs"]) for row in rows) == [(1, [b"ace"]), (2, [b"ace"])]
        else:
            assert await merge == 2
        assert await Item.objects.order_by("number").values_list("blobs", flat=True) == [[b"ace"], [b"ace"]]
