"""Queries over repeated rows without a primary key, including writes and their native refusals."""

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions import F
from hare.query.functions import Sum
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.contracts.query_contract_models import QueryContractModels


@pytest.mark.asyncio
async def test_keyless_rows_keep_multiplicity_across_operations(contract_database):
    model = QueryContractModels.without_primary_key()
    contract_database.register_live_models([model], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    records = [(1, 2), (1, 2), (1, 4), (2, 3), (2, 3)]
    await model.objects.bulk_create([model(group=group, value=value) for group, value in records])
    ordered = model.objects.order_by("group", "value")
    for bounds in (slice(None), slice(1, 4), slice(2, None), slice(0, 0)):
        query = ordered[bounds]
        expected = records[bounds]
        assert await query.count() == len(expected)
        assert await query.exists() == bool(expected)
        for chunk_size in (1, 2, 3):
            assert [(row.group, row.value) async for row in query.iterator(chunk_size)] == expected
            assert [row async for row in query.values_list("group", "value").iterator(chunk_size)] == expected
            assert [
                (row["group"], row["value"]) async for row in query.values("group", "value").iterator(chunk_size)
            ] == expected
    async with capture_queries() as counter:
        with pytest.raises(QueryError, match="distinct"):
            await model.objects.all().distinct().count()
    assert counter.count == 0
    assert await model.objects.order_by("group").values("group").distinct().count() == len(
        {group for group, value in records}
    )
    assert await model.objects.order_by("group").values_list("group", flat=True).distinct().count() == len(
        {group for group, value in records}
    )
    assert await model.objects.all().aggregate(total=Sum("value")) == {"total": sum(value for group, value in records)}

    for operation in ("update", "delete"):
        for shape in ("model", "dict"):
            query = model.objects.filter(group=1)
            write = query.update(value=F("value") + 1) if operation == "update" else query.delete()
            write = write.returning() if shape == "model" else write.returning("group", "value")
            if not contract_database.get_connection().features.supports_returning:
                async with capture_queries() as counter:
                    with pytest.raises(UnSupportedError):
                        await write
                assert counter.count == 0
                assert await ordered.values_list("group", "value") == records
                continue
            expected = [(group, value + (operation == "update")) for group, value in records if group == 1]
            rows = await write
            actual = [(row.group, row.value) if shape == "model" else (row["group"], row["value"]) for row in rows]
            assert sorted(actual) == sorted(expected)
            if shape == "model":
                assert all(row._saved_in_db for row in rows)
            records = [
                (group, value + (operation == "update" and group == 1))
                for group, value in records
                if operation != "delete" or group != 1
            ]
            assert await ordered.values_list("group", "value") == records
            if operation == "delete":
                # Each output shape must delete actual rows, including duplicate values.
                await model.objects.bulk_create([model(group=group, value=value) for group, value in expected])
                records = sorted([*records, *expected])
