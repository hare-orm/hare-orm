"""A delete cascade run in Python (soft delete, db_constraint=False relations) works in batches:
one query per relation and model per wave, split past the bind-parameter ceiling, with the same
results as deleting row by row - PROTECT/RESTRICT, SET_NULL, M2M, dispatch switches, versions,
tenants and overridden delete()."""

import os
import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest import mock

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features, truncate_all_models
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import IntegrityError, ProtectedError, StaleObjectError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.models.deletion.cascade.deletion_collector import DeletionCollector
from hare.models.tenancy.tenancy import Tenancy
from tests.deletion.cascade_batch_models import (
    BatchAuditedChild,
    BatchAuditedHoldingItem,
    BatchConstrainedAuditedChild,
    BatchConstrainedAuditedGrandchild,
    BatchConstrainedMiddle,
    BatchConstrainedParent,
    BatchCycleMiddle,
    BatchCyclePartner,
    BatchCycleTop,
    BatchHardChildOfSoft,
    BatchLooseChild,
    BatchLooseGrandchild,
    BatchLooseNullingRow,
    BatchLooseParent,
    BatchLooseProtector,
    BatchLooseRestrictor,
    BatchLooseSoftChild,
    BatchNullingRow,
    BatchSoftAuditedHolding,
    BatchSoftAuditedLeaf,
    BatchSoftChild,
    BatchSoftGrandchild,
    BatchSoftOwner,
    BatchSoftPairChild,
    BatchSoftParent,
    BatchTag,
    BatchTenantChild,
    BatchTenantParent,
)

#: Bind-parameter ceiling the batching tests lower the backend's to, so a few dozen rows cross it.
SMALL_BIND_PARAMETER_LIMIT = 150


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


@pytest_asyncio.fixture(scope="module")
async def cascade_batch_context() -> AsyncGenerator[Any]:
    async with hare_test_context(
        ["tests.deletion.cascade_batch_models"], db_url=get_test_db_url(), connection_label="models"
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def cascade_batch_db(cascade_batch_context: Any) -> AsyncGenerator[Any]:
    BatchAuditedChild.delete_override_calls.clear()
    BatchConstrainedAuditedChild.delete_override_calls.clear()
    BatchConstrainedAuditedGrandchild.delete_override_calls.clear()
    BatchCycleMiddle.delete_override_calls.clear()
    BatchCyclePartner.delete_override_calls.clear()
    BatchSoftAuditedHolding.delete_override_calls.clear()
    BatchSoftAuditedHolding.hard_delete_override_calls.clear()
    BatchAuditedHoldingItem.delete_override_calls.clear()
    BatchSoftAuditedLeaf.delete_override_calls.clear()
    yield cascade_batch_context
    await truncate_all_models()


@pytest.fixture
def small_bind_parameter_limit(cascade_batch_db: Any) -> Generator[int]:
    client_class = type(BatchSoftParent._meta.connection)
    with mock.patch.object(
        client_class, "features", client_class.features.replace(max_bind_parameters=SMALL_BIND_PARAMETER_LIMIT)
    ):
        yield SMALL_BIND_PARAMETER_LIMIT


@pytest_asyncio.fixture
async def captured_queries() -> AsyncGenerator[list[str]]:
    queries: list[str] = []

    def capture(event) -> None:
        sql = event.sql
        queries.append(sql or "")

    Observers.observe(QueryExecuted, capture)
    try:
        yield queries
    finally:
        await Observers.wait_for_pending()
        Observers.unobserve(QueryExecuted, capture)


async def create_soft_tree(parent_count: int, *, first_id: int = 0) -> None:
    parent_ids = range(first_id, first_id + parent_count)
    await BatchSoftParent.objects.bulk_create([BatchSoftParent(id=parent_id) for parent_id in parent_ids])
    await BatchSoftChild.objects.bulk_create(
        [BatchSoftChild(id=parent_id, parent_id=parent_id) for parent_id in parent_ids]
    )
    await BatchSoftGrandchild.objects.bulk_create(
        [BatchSoftGrandchild(id=parent_id, child_id=parent_id) for parent_id in parent_ids]
    )
    await BatchSoftPairChild.objects.bulk_create(
        [BatchSoftPairChild(a=parent_id, b=-parent_id, parent_id=parent_id) for parent_id in parent_ids]
    )
    await BatchHardChildOfSoft.objects.bulk_create(
        [BatchHardChildOfSoft(id=parent_id, parent_id=parent_id) for parent_id in parent_ids]
    )
    await BatchNullingRow.objects.bulk_create(
        [BatchNullingRow(id=parent_id, parent_id=parent_id) for parent_id in parent_ids]
    )


async def create_loose_tree(parent_count: int, *, first_id: int = 0) -> None:
    parent_ids = range(first_id, first_id + parent_count)
    await BatchLooseParent.objects.bulk_create([BatchLooseParent(id=parent_id) for parent_id in parent_ids])
    await BatchLooseChild.objects.bulk_create(
        [BatchLooseChild(id=parent_id, parent_id=parent_id) for parent_id in parent_ids]
    )
    await BatchLooseGrandchild.objects.bulk_create(
        [BatchLooseGrandchild(id=parent_id, child_id=parent_id) for parent_id in parent_ids]
    )
    await BatchLooseSoftChild.objects.bulk_create(
        [BatchLooseSoftChild(id=parent_id, parent_id=parent_id) for parent_id in parent_ids]
    )
    await BatchLooseNullingRow.objects.bulk_create(
        [BatchLooseNullingRow(id=parent_id, child_id=parent_id) for parent_id in parent_ids]
    )


async def count_delete_queries(queries: list[str], delete_call: Any) -> int:
    await Observers.wait_for_pending()
    queries.clear()
    await delete_call()
    await Observers.wait_for_pending()
    return len(queries)


@pytest.mark.asyncio
async def test_soft_delete_cascade_across_several_batches(small_bind_parameter_limit):
    await create_soft_tree(120)

    assert await BatchSoftParent.objects.all().delete() == 120

    assert await BatchSoftParent.objects.all().count() == 0
    assert await BatchSoftChild.objects.all().count() == 0
    assert await BatchSoftGrandchild.objects.all().count() == 0
    assert await BatchSoftPairChild.objects.all().count() == 0
    assert await BatchHardChildOfSoft.objects.all().count() == 120
    assert await BatchNullingRow.objects.filter(parent_id__isnull=True).count() == 120
    deleted_at_values = {
        *await BatchSoftParent.objects.include_deleted().values_list("deleted_at", flat=True),
        *await BatchSoftChild.objects.include_deleted().values_list("deleted_at", flat=True),
        *await BatchSoftGrandchild.objects.include_deleted().values_list("deleted_at", flat=True),
        *await BatchSoftPairChild.objects.include_deleted().values_list("deleted_at", flat=True),
    }
    assert len(deleted_at_values) == 1
    assert set(await BatchSoftParent.objects.include_deleted().values_list("version", flat=True)) == {2}
    assert set(await BatchSoftChild.objects.include_deleted().values_list("version", flat=True)) == {2}


@pytest.mark.asyncio
async def test_soft_delete_cascade_keeps_m2m_through_rows(small_bind_parameter_limit):
    await create_soft_tree(60)
    tag = await BatchTag.objects.create(id=1)
    for parent in await BatchSoftParent.objects.all():
        await parent.tags.add(tag)

    await BatchSoftParent.objects.all().delete()

    assert await tag.soft_parents.all().count() == 0
    assert await tag.soft_parents.all().include_deleted().count() == 60


@pytest.mark.asyncio
async def test_query_count_does_not_grow_with_the_row_count(cascade_batch_db, captured_queries):
    await create_soft_tree(10)
    small_soft_count = await count_delete_queries(captured_queries, BatchSoftParent.objects.all().delete)
    await create_soft_tree(300, first_id=1000)
    large_soft_count = await count_delete_queries(captured_queries, BatchSoftParent.objects.all().delete)
    assert large_soft_count == small_soft_count

    await create_loose_tree(10)
    small_loose_count = await count_delete_queries(captured_queries, BatchLooseParent.objects.all().delete)
    await create_loose_tree(300, first_id=1000)
    large_loose_count = await count_delete_queries(captured_queries, BatchLooseParent.objects.all().delete)
    assert large_loose_count == small_loose_count


@pytest.mark.asyncio
async def test_single_instance_delete_batches_its_children(cascade_batch_db, captured_queries):
    await BatchSoftParent.objects.create(id=1)
    await BatchSoftChild.objects.create(id=1, parent_id=1)
    few_children_count = await count_delete_queries(captured_queries, (await BatchSoftParent.objects.get(id=1)).delete)

    await BatchSoftParent.objects.create(id=2)
    await BatchSoftChild.objects.bulk_create([BatchSoftChild(id=index, parent_id=2) for index in range(100, 400)])
    many_children_count = await count_delete_queries(
        captured_queries, (await BatchSoftParent.objects.get(id=2)).delete
    )

    assert many_children_count == few_children_count
    assert await BatchSoftChild.objects.filter(parent_id=2).count() == 0
    assert await BatchSoftChild.objects.include_deleted().filter(parent_id=2).count() == 300


@pytest.mark.asyncio
async def test_loose_hard_cascade_across_several_batches(small_bind_parameter_limit):
    await create_loose_tree(120)

    assert await BatchLooseParent.objects.all().delete() == 120

    assert await BatchLooseParent.objects.all().count() == 0
    assert await BatchLooseChild.objects.all().count() == 0
    assert await BatchLooseGrandchild.objects.all().count() == 0
    assert await BatchLooseSoftChild.objects.all().count() == 0
    assert await BatchLooseSoftChild.objects.include_deleted().count() == 120
    assert await BatchLooseNullingRow.objects.filter(child_id__isnull=True).count() == 120


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_loose_restrict_blocks_the_whole_batched_delete(small_bind_parameter_limit):
    await create_loose_tree(120)
    await BatchLooseRestrictor.objects.create(id=1, child_id=97)

    with pytest.raises(IntegrityError):
        await BatchLooseParent.objects.all().delete()

    assert await BatchLooseParent.objects.all().count() == 120
    assert await BatchLooseChild.objects.all().count() == 120
    assert await BatchLooseNullingRow.objects.filter(child_id__isnull=True).count() == 0
    assert await BatchLooseSoftChild.objects.all().count() == 120


@pytest.mark.asyncio
async def test_protect_on_a_cascaded_row_blocks_the_whole_batched_delete(small_bind_parameter_limit):
    await create_loose_tree(120)
    await BatchLooseProtector.objects.create(id=1, child_id=64)

    with pytest.raises(ProtectedError) as raised:
        await BatchLooseParent.objects.all().delete()

    assert [row.pk for row in raised.value.protected_objects] == [1]
    assert await BatchLooseParent.objects.all().count() == 120
    assert await BatchLooseChild.objects.all().count() == 120


@pytest.mark.asyncio
async def test_overridden_delete_is_still_called_for_every_row(small_bind_parameter_limit):
    await BatchLooseParent.objects.bulk_create([BatchLooseParent(id=index) for index in range(80)])
    await BatchAuditedChild.objects.bulk_create([BatchAuditedChild(id=index, parent_id=index) for index in range(80)])

    assert await BatchLooseParent.objects.all().delete() == 80

    assert sorted(BatchAuditedChild.delete_override_calls) == list(range(80))
    assert await BatchAuditedChild.objects.all().count() == 0


@pytest.mark.asyncio
async def test_overridden_delete_behind_a_database_enforced_relation_is_called(cascade_batch_db):
    """The database's own cascade would remove these rows without calling the override."""
    parents = await BatchConstrainedParent.objects.bulk_create(
        [BatchConstrainedParent(id=index) for index in range(3)]
    )
    await BatchConstrainedAuditedChild.objects.bulk_create(
        [BatchConstrainedAuditedChild(id=index, parent_id=index) for index in range(3)]
    )
    await BatchConstrainedMiddle.objects.bulk_create(
        [BatchConstrainedMiddle(id=index, parent_id=index) for index in range(3)]
    )
    await BatchConstrainedAuditedGrandchild.objects.bulk_create(
        [BatchConstrainedAuditedGrandchild(id=index, middle_id=index) for index in range(3)]
    )

    await parents[0].delete()
    assert BatchConstrainedAuditedChild.delete_override_calls == [0]
    assert BatchConstrainedAuditedGrandchild.delete_override_calls == [0]

    assert await BatchConstrainedParent.objects.filter(id__in=[1, 2]).delete() == 2
    assert sorted(BatchConstrainedAuditedChild.delete_override_calls) == [0, 1, 2]
    assert sorted(BatchConstrainedAuditedGrandchild.delete_override_calls) == [0, 1, 2]
    assert await BatchConstrainedAuditedChild.objects.all().count() == 0
    assert await BatchConstrainedMiddle.objects.all().count() == 0
    assert await BatchConstrainedAuditedGrandchild.objects.all().count() == 0


@pytest.mark.asyncio
async def test_queryset_delete_of_rows_overriding_delete_calls_the_override(cascade_batch_db):
    """A bulk delete without a cascade run in Python - a plain DELETE, the single UPDATE of a soft
    delete, a hard delete - removed the matched rows without calling their override."""
    await BatchConstrainedParent.objects.create(id=1)
    await BatchConstrainedMiddle.objects.create(id=1, parent_id=1)
    await BatchConstrainedAuditedGrandchild.objects.bulk_create(
        [BatchConstrainedAuditedGrandchild(id=index, middle_id=1) for index in range(3)]
    )
    await BatchSoftAuditedLeaf.objects.bulk_create([BatchSoftAuditedLeaf(id=index) for index in range(3)])
    await BatchSoftOwner.objects.create(id=1)
    await BatchSoftAuditedHolding.objects.bulk_create(
        [BatchSoftAuditedHolding(id=index, owner_id=1) for index in range(3)]
    )

    assert await BatchConstrainedAuditedGrandchild.objects.filter(id__in=[0, 1]).delete() == 2
    assert await BatchSoftAuditedLeaf.objects.filter(id__in=[0, 1]).delete() == 2
    assert await BatchSoftAuditedHolding.objects.filter(id__in=[0, 1]).hard_delete() == 2

    assert sorted(BatchConstrainedAuditedGrandchild.delete_override_calls) == [0, 1]
    assert sorted(BatchSoftAuditedLeaf.delete_override_calls) == [0, 1]
    assert sorted(BatchSoftAuditedHolding.hard_delete_override_calls) == [0, 1]
    assert await BatchConstrainedAuditedGrandchild.objects.values_list("id", flat=True) == [2]
    assert await BatchSoftAuditedLeaf.objects.values_list("id", flat=True) == [2]
    assert await BatchSoftAuditedLeaf.objects.include_deleted().count() == 3
    assert await BatchSoftAuditedHolding.objects.include_deleted().values_list("id", flat=True) == [2]


@pytest.mark.asyncio
async def test_queryset_delete_returning_of_rows_overriding_delete_calls_the_override(cascade_batch_db):
    """A plain DELETE ... RETURNING removed the matched rows without calling their override."""
    await BatchConstrainedParent.objects.create(id=1)
    await BatchConstrainedMiddle.objects.create(id=1, parent_id=1)
    await BatchConstrainedAuditedGrandchild.objects.bulk_create(
        [BatchConstrainedAuditedGrandchild(id=index, middle_id=1) for index in range(3)]
    )

    returned = await BatchConstrainedAuditedGrandchild.objects.filter(id__in=[0, 1]).delete().returning("id")

    assert sorted(row["id"] for row in returned) == [0, 1]
    assert sorted(BatchConstrainedAuditedGrandchild.delete_override_calls) == [0, 1]
    assert await BatchConstrainedAuditedGrandchild.objects.values_list("id", flat=True) == [2]


@pytest.mark.parametrize("deletes_through_queryset", [False, True])
@pytest.mark.asyncio
async def test_overridden_deletes_on_a_cycle_are_called_once_per_row(cascade_batch_db, deletes_through_queryset):
    """Two models overriding delete() cascading onto each other handed their rows back and forth until the
    recursion limit."""
    top = await BatchCycleTop.objects.create(id=1)
    await BatchCycleTop.objects.create(id=2)
    await BatchCycleMiddle.objects.create(id=1, top_id=1)
    await BatchCyclePartner.objects.create(id=1, middle_id=1)
    await BatchCycleMiddle.objects.filter(id=1).update(partner_id=1)
    await BatchCycleMiddle.objects.create(id=2, top_id=2)

    if deletes_through_queryset:
        assert await BatchCycleTop.objects.filter(id=1).delete() == 1
    else:
        await top.delete()

    assert BatchCycleMiddle.delete_override_calls == [1]
    assert BatchCyclePartner.delete_override_calls == [1]
    assert await BatchCycleTop.objects.values_list("id", flat=True) == [2]
    assert await BatchCycleMiddle.objects.values_list("id", flat=True) == [2]
    assert await BatchCyclePartner.objects.count() == 0


@pytest.mark.parametrize("deletes_through_queryset", [False, True])
@pytest.mark.asyncio
async def test_a_hard_delete_takes_the_hard_delete_override_of_a_soft_deleted_row(
    cascade_batch_db, deletes_through_queryset
):
    """A hard delete handed a soft-delete model's row to its delete() - which only marked it - and the
    database's own cascade removed the rows below without their delete() override."""
    owner = await BatchSoftOwner.objects.create(id=1)
    await BatchSoftAuditedHolding.objects.create(id=2, owner_id=1)
    await BatchAuditedHoldingItem.objects.create(id=3, holding_id=2)

    if deletes_through_queryset:
        assert await BatchSoftOwner.objects.filter(id=1).hard_delete() == 1
    else:
        await owner.hard_delete()

    assert BatchSoftAuditedHolding.hard_delete_override_calls == [2]
    assert BatchSoftAuditedHolding.delete_override_calls == []
    assert BatchAuditedHoldingItem.delete_override_calls == [3]
    assert await BatchSoftAuditedHolding.objects.include_deleted().count() == 0
    assert await BatchAuditedHoldingItem.objects.count() == 0


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_version_changed_after_the_cascade_read_it_raises_stale(small_bind_parameter_limit, monkeypatch):
    await create_soft_tree(120)
    original_get_child_rows = DeletionCollector._get_child_rows

    async def bump_one_child_after_reading(
        self: DeletionCollector, plan: Any, backward_field: Any, target_values: list[Any], action: Any
    ):
        rows = await original_get_child_rows(self, plan, backward_field, target_values, action)
        if backward_field.related_model is BatchSoftChild:
            await BatchSoftChild.objects.filter(id=90).using(self.connection).update(parent_id=90)
        return rows

    monkeypatch.setattr(DeletionCollector, "_get_child_rows", bump_one_child_after_reading)

    with pytest.raises(StaleObjectError) as raised:
        await BatchSoftParent.objects.all().delete()

    assert raised.value.pk == 90
    assert await BatchSoftParent.objects.all().count() == 120
    assert await BatchSoftChild.objects.all().count() == 120
    assert await BatchHardChildOfSoft.objects.all().count() == 120


@pytest.mark.asyncio
async def test_cascade_reaches_every_tenant_in_batches(small_bind_parameter_limit):
    with Tenancy.scope(1):
        await BatchTenantParent.objects.bulk_create([BatchTenantParent(id=index, company_id=1) for index in range(60)])
    # No tenant active: a cross-tenant link is written as trusted seed data.
    for index in range(60):
        await BatchTenantChild.objects.create(id=index, company_id=2, parent_id=index)
    with Tenancy.scope(1):
        assert await BatchTenantParent.objects.all().delete() == 60

    with Tenancy.scope(2):
        assert await BatchTenantChild.objects.all().count() == 0
        assert await BatchTenantChild.objects.include_deleted().count() == 60


@pytest.mark.asyncio
async def test_already_soft_deleted_children_keep_their_deletion_time(small_bind_parameter_limit):
    await create_soft_tree(60)
    early_child = await BatchSoftChild.objects.get(id=5)
    await early_child.delete()
    early_deleted_at = (await BatchSoftChild.objects.include_deleted().get(id=5)).deleted_at

    await BatchSoftParent.objects.all().delete()

    assert (await BatchSoftChild.objects.include_deleted().get(id=5)).deleted_at == early_deleted_at
    assert await BatchSoftGrandchild.objects.all().count() == 0
