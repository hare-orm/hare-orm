"""Derived declarations borrow rows; executions and previews never modify their source."""

import asyncio
from copy import copy
from unittest.mock import patch

import pytest

from hare.query.functions import Count
from hare.query.queryset.selection.statement_selection import StatementSelection
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.statements.write.matching_rows_query import MatchingRowsQuery
from tests.shared_models.testmodels import SoftDeleteStandalone, Tournament


@pytest.mark.asyncio
async def test_derived_calls_do_not_copy_the_source(db):
    source = Tournament.objects.filter(name="borrowed")
    with patch.object(SpecificationCopying, "copy_specification", side_effect=AssertionError("eager copy")):
        operations = (
            source.count(),
            source.exists(),
            source.aggregate(total=Count("id")),
            source.update(name="new"),
            source.delete(),
        )
    for operation in operations:
        built = copy(operation)
        assert built._q_object_list is not source._q_object_list
        assert built._annotations is not source._annotations


@pytest.mark.asyncio
async def test_repeated_and_concurrent_summaries_leave_source_unchanged(db):
    await Tournament.objects.create(name="borrowed")
    source = Tournament.objects.filter(name="borrowed")
    pending = source._pending_filter_calls
    count, exists, aggregate = source.count(), source.exists(), source.aggregate(total=Count("id"))

    async def read():
        return await count, await exists, await aggregate

    assert await asyncio.gather(read(), read()) == [(1, True, {"total": 1})] * 2
    assert source._pending_filter_calls == pending
    assert source._connection is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["values", "union", "slice"])
async def test_selected_summaries_defer_building_and_support_repeat_sql(db, shape):
    await Tournament.objects.create(name="first")
    await Tournament.objects.create(name="second")
    source = Tournament.objects.order_by("id")
    if shape == "values":
        source = source.values("id", "name")
    elif shape == "union":
        source = Tournament.objects.filter(name="first").union(Tournament.objects.filter(name="second"))
    else:
        source = source.limit(1)
    with patch.object(StatementSelection, "get_rows_source", side_effect=AssertionError("eager row source")):
        count, exists, aggregate = source.count(), source.exists(), source.aggregate(total=Count("id"))
    expected = 1 if shape == "slice" else 2
    for _ in range(2):
        assert count.sql() == count.sql()
        assert await count == expected
        assert await exists is True
        assert await aggregate == {"total": expected}
    assert source._connection is None


@pytest.mark.asyncio
async def test_internal_write_narrowing_preserves_the_slice_and_source(db):
    objects = [await Tournament.objects.create(name=str(index)) for index in range(3)]
    source = Tournament.objects.order_by("id").limit(2)
    narrowed = MatchingRowsQuery._filter_matching_rows(source, pk__in=[objects[1].pk, objects[2].pk])
    assert narrowed._limit == source._limit == 2
    assert [obj.pk for obj in await source] == [obj.pk for obj in objects[:2]]
    assert [obj.pk for obj in await narrowed] == [obj.pk for obj in objects[1:]]


@pytest.mark.asyncio
async def test_reused_soft_delete_and_restore_keep_the_source_unbound(db):
    obj = await SoftDeleteStandalone.objects.create(name="soft")
    source = SoftDeleteStandalone.objects.filter(pk=obj.pk)
    delete = source.delete()
    restore = source.restore()
    assert source._call_signature is not None
    assert restore.source._call_signature == source._call_signature
    assert source.include_deleted()._call_signature == source._call_signature
    assert source.only_deleted()._call_signature == source._call_signature
    assert source.all_tenants()._call_signature == source._call_signature
    for _ in range(2):
        assert await delete == 1
        assert await restore == 1
    assert delete._connection is None
    assert source._connection is None
    assert restore.source._connection is None


@pytest.mark.asyncio
async def test_update_returning_keeps_declaration_unbound(db):
    connection = Tournament.get_connection()
    if not connection.features.supports_returning:
        pytest.skip("RETURNING is not supported")
    obj = await Tournament.objects.create(name="before")
    source = Tournament.objects.filter(pk=obj.pk)
    update = source.update(name="after").returning("name")
    assert await update == [{"name": "after"}]
    assert await update == [{"name": "after"}]
    assert update._connection is None
    assert update._prepared_write is None
    assert source._connection is None
