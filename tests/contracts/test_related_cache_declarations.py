"""Fetched relation rows belong to their connection and scope declaration."""

import sys
from types import ModuleType

import pytest
import pytest_asyncio

from hare import Model, fields, prefetch_related_objects
from hare.contrib.test import requires_features
from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.exceptions import NoValuesFetched
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.multi_database_context import MultiDatabaseTestContext


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", (False, True), ids=("scalar", "composite"))
@pytest.mark.parametrize("key_type", [CitextField, fields.FloatField], ids=["citext", "nan"])
async def test_relation_writes_use_the_databases_key_equality(contract_database, composite, key_type):
    class Peer(Model):
        code = key_type(primary_key=not composite)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "code")
        deleted_at = fields.DatetimeField(null=True)

        class Meta:
            soft_delete_field = "deleted_at"

    class Owner(Model):
        id = fields.IntField(primary_key=True, generated=False)
        peers = fields.ManyToManyField(Peer, through=f"{CONTRACT_APP}.Link")

    class Link(Model):
        number = fields.IntField(primary_key=True, generated=False)
        owner = fields.ForeignKeyField(Owner)
        peer = fields.ForeignKeyField(Peer)
        payload = fields.IntField()

        class Meta:
            table = "relation_key_comparison_link"

    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    if key_type is CitextField:
        await connection.execute_script("CREATE EXTENSION IF NOT EXISTS citext")
    contract_database.register_live_models([Owner, Peer, Link], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    code, equivalent = ("Alpha", "aLPHa") if key_type is CitextField else (float("nan"), float("nan"))
    peer = await Peer.objects.create(code=code)
    first, other, new = [await Owner.objects.create(id=number) for number in (1, 2, 3)]
    peer.code = equivalent
    await Link.objects.create(number=11, owner=first, peer=peer, payload=19)
    peer.code = code
    await Link.objects.create(number=22, owner=other, peer=peer, payload=29)
    equivalent_key = (7, equivalent) if composite else equivalent
    assert await Peer.objects.filter(pk=equivalent_key).count() == 1
    check_rows = "SELECT number, payload FROM relation_key_comparison_link ORDER BY number"

    original = [{"number": 11, "payload": 19}, {"number": 22, "payload": 29}]
    await first.peers.add(peer, through_defaults={"number": 33, "payload": 99})
    assert await connection.execute_dicts(check_rows) == original

    await first.peers.set(equivalent_key, through_defaults={"number": 33, "payload": 99})
    assert await connection.execute_dicts(check_rows) == original

    await new.peers.add(equivalent_key, through_defaults={"number": 33, "payload": 99})
    assert await connection.execute_dicts(check_rows) == [*original, {"number": 33, "payload": 99}]

    await first.peers.set(equivalent_key, clear=True, through_defaults={"number": 44, "payload": 39})
    assert await connection.execute_dicts(check_rows) == [
        {"number": 22, "payload": 29},
        {"number": 33, "payload": 99},
        {"number": 44, "payload": 39},
    ]


@pytest_asyncio.fixture
async def cached_relations(monkeypatch):
    class Parent(Model):
        region = fields.TextField(source_field="parent_region")
        number = fields.IntField(source_field="parent_number")
        pk = fields.CompositePrimaryKey("region", "number")

    class Child(Model):
        id = fields.IntField(primary_key=True, generated=False, source_field="child_number")
        label = fields.TextField()
        parent = fields.ForeignKeyField("models.Parent", related_name="children", db_constraint=False)

    class Tag(Model):
        region = fields.TextField(source_field="tag_region")
        number = fields.IntField(source_field="tag_number")
        pk = fields.CompositePrimaryKey("region", "number")
        label = fields.TextField()
        parents = fields.ManyToManyField(
            "models.Parent", related_name="tags", db_constraint=False, through="models.Link"
        )

    class Link(Model):
        parent = fields.ForeignKeyField(Parent, related_name="tag_links", db_constraint=False)
        tag = fields.ForeignKeyField(Tag, related_name="parent_links", db_constraint=False)
        pk = fields.CompositePrimaryKey("parent", "tag")
        payload = fields.TextField()

        class Meta:
            table = "cached_relation_link"

    module = ModuleType("tests._related_cache_declarations")
    module.__models__ = [Parent, Child, Tag, Link]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    apps = {"models": {"models": [module.__name__], "default_connection": "main"}}
    async with MultiDatabaseTestContext.open(DatabaseUnderTest.get_url(), ["main", "other"], apps=apps) as context:
        await context.generate_schemas()
        main, other = context.connections.get("main"), context.connections.get("other")
        await other.execute_script(main.get_schema_sql(safe=True))
        for client, label in ((main, "main"), (other, "other")):
            parent = await Parent.objects.using(client).create(region="r", number=1)
            await Child.objects.using(client).create(id=1, label=label, parent=parent)
            tag = await Tag.objects.using(client).create(region="t", number=1, label=label)
            await parent.tags.add(tag, using=client, through_defaults={"payload": label})
            for number in (2, 3):
                await Tag.objects.using(client).create(region="t", number=number, label=f"added {number}")
        yield Parent, main, other


@pytest.mark.asyncio
@pytest.mark.parametrize("relation_name", ["children", "tags"])
@pytest.mark.parametrize("prefetched", [False, True], ids=["cold", "prefetched"])
async def test_connection_clones_cannot_read_or_publish_in_another_relation_cache(
    cached_relations, relation_name, prefetched
):
    parent_model, main, other = cached_relations
    parent = await parent_model.objects.using(main).get(pk=("r", 1))
    if prefetched:
        await prefetch_related_objects([parent], relation_name)
    original = getattr(parent, relation_name)
    for client, label in ((other, "other"), (main, "main"), (other, "other")):
        declared = original.using(client)
        assert [obj.label for obj in await declared] == [label]
        assert [obj.label async for obj in declared] == [label]
        assert [obj.label async for obj in declared] == [label]
        assert [obj.label async for obj in original] == ["main"]
    table = "child" if relation_name == "children" else "tag"
    suffix = "" if relation_name == "children" else " WHERE tag_number=1"
    assert await main.execute_dicts(f"SELECT label FROM {table}{suffix}") == [{"label": "main"}]
    assert await other.execute_dicts(f"SELECT label FROM {table}{suffix}") == [{"label": "other"}]


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
@pytest.mark.parametrize("relation_name", ["children", "tags"])
async def test_clone_writes_and_savepoint_rollback_invalidate_the_source_cache(cached_relations, relation_name):
    parent_model, main, other = cached_relations
    parent = await parent_model.objects.using(main).get(pk=("r", 1))
    await prefetch_related_objects([parent], relation_name)
    original = getattr(parent, relation_name)
    declared = original.using(other)
    key = {"id": 2} if relation_name == "children" else {"region": "t", "number": 2}
    if relation_name == "children":
        await declared.create(**key, label="added 2")
    else:
        await declared.add(("t", 2), through_defaults={"payload": "kept"})
    with pytest.raises(NoValuesFetched):
        list(original)
    assert sorted([obj.label async for obj in declared]) == ["added 2", "other"]
    assert [obj.label async for obj in original] == ["main"]
    async with other._in_transaction() as outer:
        with pytest.raises(RuntimeError, match="rollback relation write"):
            async with outer._in_transaction() as inner:
                changed = original.using(inner)
                key = {"id": 3} if relation_name == "children" else {"region": "t", "number": 3}
                if relation_name == "children":
                    await changed.create(**key, label="added 3")
                else:
                    await changed.add(("t", 3), through_defaults={"payload": "rolled back"})
                assert sorted([obj.label async for obj in changed]) == ["added 2", "added 3", "other"]
                await prefetch_related_objects([parent], relation_name)
                raise RuntimeError("rollback relation write")
        with pytest.raises(NoValuesFetched):
            list(original)
        assert await original.using(outer).count() == 2
    assert sorted([obj.label async for obj in original.using(other)]) == ["added 2", "other"]
    if relation_name == "tags":
        sql = (
            "SELECT parent_region, parent_number, tag_region, tag_number, payload "
            "FROM cached_relation_link ORDER BY tag_number"
        )
        first = {"parent_region": "r", "parent_number": 1, "tag_region": "t", "tag_number": 1}
        assert await main.execute_dicts(sql) == [{**first, "payload": "main"}]
        assert await other.execute_dicts(sql) == [
            {**first, "payload": "other"},
            {**first, "tag_number": 2, "payload": "kept"},
        ]
