"""save() edge cases: force_create on an instance that already has a pk, conflicting force flags,
empty update_fields, a pk left None whose field has a default, and a save with no column left
to write."""

import os
import uuid

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import requires_features, truncate_all_models
from hare.contrib.test.helpers import hare_test_context
from hare.exceptions import IntegrityError, QueryError
from hare.models import Model
from hare.transactions.transactions import Transactions


class SaveAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    age = fields.IntField(null=True)

    class Meta:
        table = "save_edge_author"


class SaveUuidItem(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "save_edge_uuid_item"


class SaveCodeItem(Model):
    code = fields.CharField(max_length=40, primary_key=True, default=lambda: uuid.uuid4().hex)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "save_edge_code_item"


class SaveOnlyPk(Model):
    id = fields.IntField(primary_key=True, generated=False)

    class Meta:
        table = "save_edge_only_pk"


class SaveDbDefaultOnly(Model):
    id = fields.IntField(primary_key=True, generated=False)
    note = fields.CharField(max_length=20, db_default="n")

    class Meta:
        table = "save_edge_db_default_only"


@pytest_asyncio.fixture(scope="module")
async def save_context():
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with hare_test_context(
        modules=[__name__], db_url=db_url, app_label="models", connection_label="models"
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def save_db(save_context):
    yield save_context
    await truncate_all_models()


@pytest.mark.asyncio
async def test_force_create_on_saved_instance_with_generated_pk_raises(save_db):
    author = await SaveAuthor.objects.create(name="x")
    original_pk = author.pk

    with pytest.raises(IntegrityError):
        await author.save(force_create=True)
    assert author.pk == original_pk
    assert await SaveAuthor.objects.all().values_list("id", flat=True) == [original_pk]

    author.name = "updated"
    await author.save()
    assert await SaveAuthor.objects.all().values_list("id", "name") == [(original_pk, "updated")]


@pytest.mark.asyncio
async def test_force_create_on_fetched_instance_with_generated_pk_raises(save_db):
    author = await SaveAuthor.objects.create(name="x")
    fetched = await SaveAuthor.objects.get(pk=author.pk)

    with pytest.raises(IntegrityError):
        await fetched.save(force_create=True)
    assert await SaveAuthor.objects.all().count() == 1


@pytest.mark.asyncio
async def test_force_create_reinserts_deleted_row_under_its_pk(save_db):
    author = await SaveAuthor.objects.create(name="x")
    original_pk = author.pk
    await SaveAuthor.objects.filter(pk=original_pk).delete()

    await author.save(force_create=True)
    assert author.pk == original_pk
    assert await SaveAuthor.objects.all().values_list("id", "name") == [(original_pk, "x")]


@pytest.mark.asyncio
async def test_force_create_without_pk_still_generates_one(save_db):
    first = SaveAuthor(name="a")
    await first.save(force_create=True)
    second = SaveAuthor(name="b")
    await second.save(force_create=True)
    assert first.pk is not None
    assert second.pk is not None
    assert first.pk != second.pk


@pytest.mark.asyncio
async def test_force_create_and_force_update_together_raise(save_db):
    with pytest.raises(QueryError):
        await SaveAuthor(id=11, name="x").save(force_create=True, force_update=True)
    assert await SaveAuthor.objects.all().count() == 0


@pytest.mark.asyncio
async def test_empty_update_fields_on_unsaved_instance_is_a_no_op(save_db):
    author = SaveAuthor(name="x")
    await author.save(update_fields=[])
    assert author.pk is None
    assert not author._saved_in_db
    assert await SaveAuthor.objects.all().count() == 0


@pytest.mark.asyncio
async def test_empty_update_fields_on_saved_and_partial_instances_is_a_no_op(save_db):
    author = await SaveAuthor.objects.create(name="x", age=1)
    author.name = "local"
    await author.save(update_fields=())
    assert await SaveAuthor.objects.filter(pk=author.pk).values_list("name", flat=True) == ["x"]

    partial = await SaveAuthor.objects.filter(pk=author.pk).only("id").get()
    await partial.save(update_fields=[])
    assert await SaveAuthor.objects.filter(pk=author.pk).values_list("name", "age") == [("x", 1)]


@pytest.mark.asyncio
async def test_copy_with_pk_none_applies_uuid_default(save_db):
    item = await SaveUuidItem.objects.create(name="u")
    original_pk = item.pk
    item.pk = None

    await item.save()
    assert isinstance(item.pk, uuid.UUID)
    assert item.pk != original_pk
    assert await SaveUuidItem.objects.all().count() == 2
    assert (await SaveUuidItem.objects.get(pk=item.pk)).name == "u"


@pytest.mark.asyncio
async def test_copy_with_pk_none_applies_callable_default(save_db):
    item = await SaveCodeItem.objects.create(name="c")
    original_pk = item.pk
    item.pk = None

    await item.save()
    assert item.pk is not None
    assert item.pk != original_pk
    assert await SaveCodeItem.objects.all().count() == 2


@pytest.mark.asyncio
async def test_unsaved_instance_with_pk_none_applies_default_on_save(save_db):
    item = SaveUuidItem(name="u")
    item.pk = None
    await item.save()
    assert item.pk is not None
    assert await SaveUuidItem.objects.filter(pk=item.pk).exists()


@pytest.mark.asyncio
async def test_explicit_none_pk_kwarg_applies_default(save_db):
    constructed = SaveUuidItem(id=None, name="a")
    assert isinstance(constructed.pk, uuid.UUID)
    await constructed.save()

    created = await SaveUuidItem.objects.create(pk=None, name="b")
    assert isinstance(created.pk, uuid.UUID)

    code_item = await SaveCodeItem.objects.create(code=None, name="c")
    assert code_item.pk is not None
    assert await SaveUuidItem.objects.all().count() == 2


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_rolled_back_copy_restores_unset_pk(save_db):
    item = await SaveUuidItem.objects.create(name="u")
    item.pk = None
    with pytest.raises(RuntimeError):
        async with Transactions.atomic("models"):
            await item.save()
            raise RuntimeError("rollback")
    assert item.pk is None
    assert await SaveUuidItem.objects.all().count() == 1


@pytest.mark.asyncio
async def test_force_update_of_missing_pk_only_row_raises(save_db):
    with pytest.raises(IntegrityError):
        await SaveOnlyPk(id=1).save(force_update=True)
    assert await SaveOnlyPk.objects.all().count() == 0


@pytest.mark.asyncio
async def test_force_update_of_existing_pk_only_row_succeeds(save_db):
    await SaveOnlyPk.objects.create(id=1)
    instance = SaveOnlyPk(id=1)
    await instance.save(force_update=True)
    assert instance._saved_in_db
    assert await SaveOnlyPk.objects.all().count() == 1


@pytest.mark.asyncio
async def test_save_of_deleted_pk_only_row_raises(save_db):
    await SaveOnlyPk.objects.create(id=2)
    fetched = await SaveOnlyPk.objects.get(id=2)
    await SaveOnlyPk.objects.filter(id=2).delete()

    with pytest.raises(IntegrityError):
        await fetched.save()
    assert await SaveOnlyPk.objects.all().count() == 0


@pytest.mark.asyncio
async def test_save_of_existing_pk_only_row_succeeds(save_db):
    await SaveOnlyPk.objects.create(id=3)
    fetched = await SaveOnlyPk.objects.get(id=3)
    await fetched.save()
    assert await SaveOnlyPk.objects.all().count() == 1


@pytest.mark.asyncio
async def test_force_update_with_only_db_default_columns_checks_row(save_db):
    with pytest.raises(IntegrityError):
        await SaveDbDefaultOnly(id=1).save(force_update=True)

    await SaveDbDefaultOnly.objects.create(id=2)
    await SaveDbDefaultOnly(id=2).save(force_update=True)
    assert await SaveDbDefaultOnly.objects.all().values_list("id", "note") == [(2, "n")]
