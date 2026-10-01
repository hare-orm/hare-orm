"""A PROTECT or RESTRICT row the same cascade removes doesn't block it - also where hare runs the
cascade itself (db_constraint=False), wave by wave, and the guard sits beside or below the row it
guards."""

import os
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test.helpers import hare_test_context
from hare.exceptions import IntegrityError, ProtectedError
from tests.cascade_guard_models import LooseGuardedNode, LooseRestrictedNode


@pytest_asyncio.fixture
async def guard_models() -> AsyncGenerator[Any]:
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(["tests.cascade_guard_models"], db_url=db_url) as context:
        yield context


@pytest.mark.parametrize("guard_below_the_guarded_row", [False, True])
@pytest.mark.parametrize("via_queryset", [False, True])
@pytest.mark.asyncio
async def test_a_protector_inside_the_tree_does_not_block(guard_models, guard_below_the_guarded_row, via_queryset):
    root = await LooseGuardedNode.objects.create(name="root")
    guarded = await LooseGuardedNode.objects.create(name="guarded", parent=root)
    await LooseGuardedNode.objects.create(
        name="guard", parent=guarded if guard_below_the_guarded_row else root, guardian=guarded
    )
    survivor = await LooseGuardedNode.objects.create(name="survivor")
    if via_queryset:
        await LooseGuardedNode.objects.filter(pk=root.pk).delete()
    else:
        await root.delete()
    assert await LooseGuardedNode.objects.all().values_list("pk", flat=True) == [survivor.pk]


@pytest.mark.asyncio
async def test_a_protector_outside_the_tree_still_blocks(guard_models):
    root = await LooseGuardedNode.objects.create(name="root")
    guarded = await LooseGuardedNode.objects.create(name="guarded", parent=root)
    await LooseGuardedNode.objects.create(name="outside", guardian=guarded)
    with pytest.raises(ProtectedError):
        await root.delete()


@pytest.mark.parametrize("blocker_below_the_blocked_row", [False, True])
@pytest.mark.asyncio
async def test_a_restricting_row_inside_the_tree_does_not_block(guard_models, blocker_below_the_blocked_row):
    root = await LooseRestrictedNode.objects.create(name="root")
    blocked = await LooseRestrictedNode.objects.create(name="blocked", parent=root)
    await LooseRestrictedNode.objects.create(
        name="blocker", parent=blocked if blocker_below_the_blocked_row else root, blocker=blocked
    )
    await root.delete()
    assert await LooseRestrictedNode.objects.all().count() == 0


@pytest.mark.asyncio
async def test_a_restricting_row_outside_the_tree_still_blocks(guard_models):
    root = await LooseRestrictedNode.objects.create(name="root")
    blocked = await LooseRestrictedNode.objects.create(name="blocked", parent=root)
    await LooseRestrictedNode.objects.create(name="outside", blocker=blocked)
    with pytest.raises(IntegrityError):
        await root.delete()
