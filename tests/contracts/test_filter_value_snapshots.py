"""Filter calls and pending writes retain container values independently of later mutations."""

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context
from hare.query.expressions import Q
from tests.contracts.filter_snapshot_models import CompositeSnapshotItem, SnapshotItem
from tests.contracts.snapshot_containers import JsonDocument, KeyList, KeySet
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.first_key_list import FirstKeyList
from tests.utils.first_key_set import FirstKeySet


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
