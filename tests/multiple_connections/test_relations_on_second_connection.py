"""An instance read from a second connection keeps its relations there: reverse foreign key writes
and many-to-many prefetch run on the connection the instance came from, not on the model's
default connection."""

from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields, prefetch_related_objects
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model
from tests.utils.multi_database_context import MultiDatabaseTestContext

MODULE_NAME = "tests._relations_on_second_connection_models"


@pytest_asyncio.fixture
async def second_connection():
    class SecondOwner(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=20)

        class Meta:
            app = "second_connection"

    class SecondPet(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=20)
        owner = fields.ForeignKeyField("second_connection.SecondOwner", related_name="pets", null=True)

        class Meta:
            app = "second_connection"

    class SecondTag(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=20)
        owners = fields.ManyToManyField("second_connection.SecondOwner", related_name="tags")

        class Meta:
            app = "second_connection"

    class SecondPair(Model):
        left = fields.IntField()
        right = fields.IntField()
        label = fields.CharField(max_length=20)
        pk = CompositePrimaryKey("left", "right")

        class Meta:
            app = "second_connection"

    class SecondPairTag(Model):
        id = fields.IntField(primary_key=True)
        name = fields.CharField(max_length=20)
        pairs = fields.ManyToManyField("second_connection.SecondPair", related_name="pair_tags")

        class Meta:
            app = "second_connection"

    models = (SecondOwner, SecondPet, SecondTag, SecondPair, SecondPairTag)
    module = types.ModuleType(MODULE_NAME)
    for model in models:
        setattr(module, model.__name__, model)
    sys.modules[MODULE_NAME] = module

    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["first", "second"],
            apps={"second_connection": {"models": [MODULE_NAME], "default_connection": "first"}},
        ) as ctx:
            await ctx.generate_schemas()
            first = ctx.connections.get("first")
            second = ctx.connections.get("second")
            await second.execute_script(first.get_schema_sql(safe=True))
            yield ctx, types.SimpleNamespace(**{model.__name__: model for model in models})
    finally:
        sys.modules.pop(MODULE_NAME, None)


async def count_rows(model, connection):
    return await model.objects.all().using(connection).count()


@pytest.mark.asyncio
async def test_reverse_foreign_key_writes_follow_the_instance(second_connection):
    ctx, models = second_connection
    first = ctx.connections.get("first")
    second = ctx.connections.get("second")
    await models.SecondOwner.objects.using(second).create(name="owner")
    owner = await models.SecondOwner.objects.all().using(second).get(name="owner")

    created = await owner.pets.create(name="created")
    assert created.owner_id == owner.id
    _, created_now = await owner.pets.get_or_create(name="got")
    _, created_again = await owner.pets.get_or_create(name="got")
    assert (created_now, created_again) == (True, False)
    await owner.pets.update_or_create(name="got", defaults={"name": "updated"})
    assert sorted(await owner.pets.all().values_list("name", flat=True)) == ["created", "updated"]

    loose = await models.SecondPet.objects.using(second).create(name="loose")
    await owner.pets.add(loose)
    await owner.pets.remove(created)
    assert sorted(await owner.pets.all().values_list("name", flat=True)) == ["loose", "updated"]
    await owner.pets.set([created], bulk=False)
    assert await owner.pets.all().values_list("name", flat=True) == ["created"]
    await owner.pets.clear(bulk=False)
    assert await owner.pets.all().count() == 0

    assert await count_rows(models.SecondPet, first) == 0
    assert await count_rows(models.SecondPet, second) == 3


@pytest.mark.asyncio
async def test_many_to_many_prefetch_follows_the_parent_rows(second_connection):
    ctx, models = second_connection
    second = ctx.connections.get("second")
    owner = await models.SecondOwner.objects.using(second).create(name="owner")
    tag = await models.SecondTag.objects.using(second).create(name="tag")
    await tag.owners.add(owner, using=second)

    loaded_owner = await models.SecondOwner.objects.all().using(second).get(name="owner")
    await prefetch_related_objects([loaded_owner], "tags")
    assert [each.name for each in loaded_owner.tags] == ["tag"]

    loaded_tag = await models.SecondTag.objects.all().using(second).get(name="tag")
    await prefetch_related_objects([loaded_tag], "owners")
    assert [each.name for each in loaded_tag.owners] == ["owner"]

    prefetched = await models.SecondOwner.objects.all().using(second).prefetch_related("tags")
    assert [[each.name for each in item.tags] for item in prefetched] == [["tag"]]


@pytest.mark.asyncio
async def test_many_to_many_prefetch_with_composite_key_on_second_connection(second_connection):
    ctx, models = second_connection
    second = ctx.connections.get("second")
    pair = await models.SecondPair.objects.using(second).create(left=1, right=2, label="pair")
    pair_tag = await models.SecondPairTag.objects.using(second).create(name="pair tag")
    await pair_tag.pairs.add(pair, using=second)

    loaded_pair = await models.SecondPair.objects.all().using(second).get(pk=(1, 2))
    await prefetch_related_objects([loaded_pair], "pair_tags")
    assert [each.name for each in loaded_pair.pair_tags] == ["pair tag"]

    prefetched = await models.SecondPairTag.objects.all().using(second).prefetch_related("pairs")
    assert [[each.label for each in item.pairs] for item in prefetched] == [["pair"]]
