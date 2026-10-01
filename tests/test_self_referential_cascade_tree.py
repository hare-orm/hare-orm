"""The rows a delete reaches through a relation of a model onto itself are read with one recursive
query and laid out in the same waves the wave by wave walk gives - cycles, branches and rows deleted
already included - and the tree's soft deletes are written at once."""

import datetime

import pytest

from hare.contrib.test import capture_queries
from hare.models.deletion.deletion_collector import DeletionCollector
from hare.models.deletion.deletion_plan import DeletionPlan
from hare.models.enums import DeletionAction
from tests.testmodels import HardDeleteSelfReferentialChain, SoftDeleteSelfReferentialChain

#: id -> parent id: two branches below 1, a row pointing back at the root, a row deleted already
#: (5) with a live child below it, and an unrelated tree.
TREE = {1: 3, 2: 1, 3: 2, 4: 1, 5: 4, 6: 5, 7: 4, 8: None, 9: 8}
DELETED_ALREADY = {5}


async def create_tree(model) -> None:
    deleted_at = {"deleted_at": datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)}
    await model.objects.bulk_create(
        [
            model(
                id=row_id,
                name=f"row-{row_id}",
                **(deleted_at if model._meta.soft_delete_field and row_id in DELETED_ALREADY else {}),
            )
            for row_id in TREE
        ]
    )
    rows = model.objects.include_deleted() if model._meta.soft_delete_field else model.objects.all()
    for row_id, parent_id in TREE.items():
        await rows.filter(id=row_id).update(parent_id=parent_id)


async def collect(model, root_action: DeletionAction | None, *, walks_tree: bool, monkeypatch) -> DeletionPlan:
    if not walks_tree:

        async def not_walked(self, plan):
            return False

        monkeypatch.setattr(DeletionCollector, "_collect_tree_through_self", not_walked)
    try:
        return await DeletionCollector(model.get_connection(), root_action=root_action, reads_versions=True).collect(
            model, [1]
        )
    finally:
        monkeypatch.undo()


def get_layout(plan: DeletionPlan) -> tuple:
    return (
        [{model: sorted(rows.items()) for model, rows in wave.items()} for wave in plan.waves],
        plan.roots_reached_again,
        plan.row_versions,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("root_action", [DeletionAction.SOFT_DELETE, None])
async def test_soft_delete_tree_matches_the_wave_walk(db, monkeypatch, root_action):
    await create_tree(SoftDeleteSelfReferentialChain)
    tree_plan = await collect(SoftDeleteSelfReferentialChain, root_action, walks_tree=True, monkeypatch=monkeypatch)
    wave_plan = await collect(SoftDeleteSelfReferentialChain, root_action, walks_tree=False, monkeypatch=monkeypatch)
    assert get_layout(tree_plan) == get_layout(wave_plan)
    assert tree_plan.roots_reached_again == {1}
    if root_action is DeletionAction.SOFT_DELETE:
        assert tree_plan.waves[2][SoftDeleteSelfReferentialChain][5] is DeletionAction.UNCHANGED


@pytest.mark.asyncio
async def test_hard_delete_tree_matches_the_wave_walk(db, monkeypatch):
    await create_tree(HardDeleteSelfReferentialChain)
    tree_plan = await collect(
        HardDeleteSelfReferentialChain, DeletionAction.DELETE, walks_tree=True, monkeypatch=monkeypatch
    )
    wave_plan = await collect(
        HardDeleteSelfReferentialChain, DeletionAction.DELETE, walks_tree=False, monkeypatch=monkeypatch
    )
    assert get_layout(tree_plan) == get_layout(wave_plan)


@pytest.mark.asyncio
async def test_a_soft_deleted_tree_is_written_at_once(db):
    rows = [SoftDeleteSelfReferentialChain(id=1, name="row-1")]
    rows.extend(
        SoftDeleteSelfReferentialChain(id=row_id, name=f"row-{row_id}", parent_id=row_id - 1)
        for row_id in range(2, 301)
    )
    await SoftDeleteSelfReferentialChain.objects.bulk_create(rows)
    root = await SoftDeleteSelfReferentialChain.objects.get(id=1)
    async with capture_queries() as counter:
        await root.delete()
    assert counter.count <= 6
    deleted_times = set(
        await SoftDeleteSelfReferentialChain.objects.only_deleted().values_list("deleted_at", flat=True)
    )
    assert deleted_times == {root.deleted_at}
    assert await SoftDeleteSelfReferentialChain.objects.only_deleted().count() == 300
