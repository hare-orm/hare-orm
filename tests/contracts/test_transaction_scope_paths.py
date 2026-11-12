"""Nested transaction outcomes preserve tenant isolation, soft deletion and callback lifetimes."""

from collections import Counter

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context, requires_features
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions import Transactions
from tests.contracts.enums import ScopedWritePath
from tests.contracts.transaction_scope_models import CompositeScopedItem, ScopedItem
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.scoped_write_actions import ScopedWriteActions


class RollbackRequested(Exception):
    pass


@pytest_asyncio.fixture(scope="module")
async def scoped_rows():
    async with hare_test_context(["tests.contracts.transaction_scope_models"], db_url=DatabaseUnderTest.get_url()):
        yield


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("model", (ScopedItem, CompositeScopedItem))
@pytest.mark.parametrize("path", ScopedWritePath)
async def test_nested_outcomes_keep_scoped_writes_and_callbacks_together(scoped_rows, model, path):
    for inner_rollback in (False, True):
        for outer_rollback in (False, True):
            await model.objects.all_tenants().include_deleted().hard_delete()
            for tenant_id, number, amount in ((1, 1, 10), (1, 2, 20), (2, 3, 30)):
                with Tenancy.scope(tenant_id):
                    await model.objects.create(id=number, tenant_id=tenant_id, amount=amount)
            keys = [(1, number) if model is CompositeScopedItem else number for number in (1, 2)]
            events = []
            with Tenancy.scope(1):
                try:
                    async with Transactions.atomic():
                        await ScopedWriteActions.change(model, keys[0], 3, path)
                        Transactions.on_commit(lambda: events.append("outer_commit"))
                        Transactions.on_rollback(lambda: events.append("outer_rollback"))
                        try:
                            async with Transactions.atomic():
                                await ScopedWriteActions.change(model, keys[1], 5, path)
                                Transactions.on_commit(lambda: events.append("inner_commit"))
                                Transactions.on_rollback(lambda: events.append("inner_rollback"))
                                if inner_rollback:
                                    raise RollbackRequested
                        except RollbackRequested:
                            pass
                        if outer_rollback:
                            raise RollbackRequested
                except RollbackRequested:
                    pass
            rows = await model.objects.all_tenants().include_deleted().order_by("tenant_id", "id")
            actual = [(obj.tenant_id, obj.id, obj.amount, obj.deleted_at is not None) for obj in rows]
            committed = [not outer_rollback, not outer_rollback and not inner_rollback]
            expected = [
                (
                    1,
                    number,
                    base + (increment if committed[number - 1] and path is not ScopedWritePath.DELETE else 0),
                    committed[number - 1] and path is ScopedWritePath.DELETE,
                )
                for number, base, increment in ((1, 10, 3), (2, 20, 5))
            ] + [(2, 3, 30, False)]
            label = (path, inner_rollback, outer_rollback)
            assert actual == expected, label
            expected_events = ["inner_rollback"] if inner_rollback else []
            expected_events += ["outer_rollback"] if outer_rollback else ["outer_commit"]
            if not inner_rollback:
                expected_events += ["inner_rollback" if outer_rollback else "inner_commit"]
            assert Counter(events) == Counter(expected_events), label
