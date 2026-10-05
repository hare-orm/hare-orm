"""save(changed_only=True): only the fields changed since the instance was loaded or saved are written -
nothing for no change, an unsaved instance inserted whole, a relation by its key, the version and
auto_now fields following - and the calls it refuses."""

from __future__ import annotations

import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import QueryError
from tests.testmodels import (
    DirtyTrackedChild,
    DirtyTrackedComposite,
    DirtyTrackedParent,
    DirtyTrackedThing,
    IntFields,
    SoftDeleteVersionedDirtyTracked,
)


def get_updates(queries) -> list[str]:
    # Quoted alike on every database.
    return [sql.replace("`", '"') for sql in queries.queries if sql.lstrip().upper().startswith("UPDATE")]


@pytest.mark.asyncio
async def test_only_the_changed_fields_are_written(db):
    thing = await DirtyTrackedThing.objects.create(name="a", count=1, nullable="x")
    thing.count = 2
    async with capture_queries() as queries:
        await thing.save(changed_only=True)
    (update,) = get_updates(queries)
    assert '"count"' in update
    assert '"name"' not in update and '"nullable"' not in update and '"data"' not in update
    loaded = await DirtyTrackedThing.objects.get(id=thing.id)
    assert (loaded.name, loaded.count, loaded.nullable) == ("a", 2, "x")
    loaded.data = {"k": 1}
    loaded.nullable = None
    async with capture_queries() as queries:
        await loaded.save(changed_only=True)
    (update,) = get_updates(queries)
    assert '"data"' in update and '"nullable"' in update and '"count"' not in update
    assert (await DirtyTrackedThing.objects.get(id=thing.id)).data == {"k": 1}


@pytest.mark.asyncio
async def test_no_change_sends_nothing(db):
    thing = await DirtyTrackedThing.objects.create(name="a")
    async with capture_queries() as queries:
        await thing.save(changed_only=True)
        thing.count = thing.count
        await thing.save(changed_only=True)
    assert queries.count == 0


@pytest.mark.asyncio
async def test_an_unsaved_instance_is_inserted_whole(db):
    thing = DirtyTrackedThing(name="new", count=5)
    await thing.save(changed_only=True)
    loaded = await DirtyTrackedThing.objects.get(id=thing.id)
    assert (loaded.name, loaded.count) == ("new", 5)
    thing.name = "renamed"
    await thing.save(changed_only=True)
    assert (await DirtyTrackedThing.objects.get(id=thing.id)).name == "renamed"


@pytest.mark.asyncio
async def test_a_relation_a_composite_key_and_a_partial_instance(db):
    first = await DirtyTrackedParent.objects.create(name="first")
    second = await DirtyTrackedParent.objects.create(name="second")
    child = await DirtyTrackedChild.objects.create(name="child", parent=first)
    child.parent = second
    async with capture_queries() as queries:
        await child.save(changed_only=True)
    (update,) = get_updates(queries)
    assert '"parent_id"' in update and '"name"' not in update
    assert (await DirtyTrackedChild.objects.get(id=child.id)).parent_id == second.id
    composite = await DirtyTrackedComposite.objects.create(a=1, b=2, name="x")
    composite.name = "y"
    await composite.save(changed_only=True)
    assert (await DirtyTrackedComposite.objects.get(a=1, b=2)).name == "y"
    partial = await DirtyTrackedThing.objects.only("id", "count").get(
        id=(await DirtyTrackedThing.objects.create(name="p")).id
    )
    partial.count = 9
    await partial.save(changed_only=True)
    assert (await DirtyTrackedThing.objects.get(id=partial.id)).count == 9


@pytest.mark.asyncio
async def test_the_version_and_auto_now_follow(db):
    row = await SoftDeleteVersionedDirtyTracked.objects.create(name="a")
    modified, version = row.modified, row.version
    row.name = "b"
    await row.save(changed_only=True)
    loaded = await SoftDeleteVersionedDirtyTracked.objects.get(id=row.id)
    assert (loaded.name, loaded.version) == ("b", version + 1)
    assert loaded.modified >= modified


@pytest.mark.asyncio
async def test_force_update(db):
    thing = await DirtyTrackedThing.objects.create(name="a")
    copy = DirtyTrackedThing(id=thing.id, name="a", count=7)
    await copy.save(changed_only=True, force_update=True)
    assert (await DirtyTrackedThing.objects.get(id=thing.id)).count == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"changed_only": "yes"}, "takes a bool"),
        ({"changed_only": True, "update_fields": ["count"]}, "takes neither update_fields nor force_create"),
        ({"changed_only": True, "force_create": True}, "takes neither update_fields nor force_create"),
    ],
)
async def test_a_wrong_call_is_refused(db, kwargs, message):
    thing = await DirtyTrackedThing.objects.create(name="a")
    with pytest.raises(QueryError, match=message):
        await thing.save(**kwargs)


@pytest.mark.asyncio
async def test_a_model_without_dirty_tracking_is_refused(db):
    row = await IntFields.objects.create(intnum=1)
    with pytest.raises(QueryError, match="needs IntFields.Meta.track_dirty_fields"):
        await row.save(changed_only=True)
