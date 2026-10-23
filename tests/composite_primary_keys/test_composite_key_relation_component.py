"""A foreign key as a component of a composite primary key - named by its key column (``book_id``) or
by itself (``book``): the relation's key column(s) take its place in the key, a relation to a model
with a composite key adding each of its columns."""

import os

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import RollbackIsolation, hare_test_context
from hare.exceptions import ConfigurationError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model
from hare.query.functions import Count
from tests.composite_primary_keys.composite_key_relation_models import (
    RelationKeyBook,
    RelationKeyChapter,
    RelationKeyEdition,
    RelationKeyNote,
    RelationKeyShelfSlot,
)

MODULES = ["tests.composite_primary_keys.composite_key_relation_models"]


@pytest_asyncio.fixture(scope="module")
async def relation_key_db_module():
    async with hare_test_context(
        MODULES,
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture
async def relation_key_db(relation_key_db_module):
    async with RollbackIsolation(relation_key_db_module) as context:
        yield context


async def create_books():
    first = await RelationKeyBook.objects.create(id=1, title="first")
    second = await RelationKeyBook.objects.create(id=2, title="second")
    await RelationKeyEdition.objects.bulk_create(
        [
            RelationKeyEdition(book=first, number=1, pages=100),
            RelationKeyEdition(book=first, number=2, pages=None),
            RelationKeyEdition(book=second, number=1, pages=50),
        ]
    )
    return first, second


def test_the_relation_columns_are_the_key(relation_key_db):
    assert RelationKeyEdition._meta.primary_key_attribute == ("book_id", "number")
    assert RelationKeyShelfSlot._meta.primary_key_attribute == ("slot", "book_id")
    assert RelationKeyNote._meta.primary_key_attribute == ("edition_book_id", "edition_number", "position")


@pytest.mark.asyncio
async def test_a_row_is_read_written_and_deleted_by_its_key(relation_key_db):
    first, _second = await create_books()

    edition = await RelationKeyEdition.objects.get(pk=(1, 2))
    assert edition.pk == (1, 2)
    assert (await edition.book).title == "first"
    edition.pages = 7
    await edition.save()
    assert await RelationKeyEdition.objects.filter(pk=(1, 2)).values_list("pages", flat=True) == [7]
    assert await RelationKeyEdition.objects.filter(pk__in=[(1, 1), (2, 1)]).order_by("book_id").values_list(
        "pages", flat=True
    ) == [100, 50]
    await RelationKeyEdition.objects.filter(pk=(1, 1)).delete()
    assert await RelationKeyEdition.objects.order_by("book_id", "number").values_list("book_id", "number") == [
        (1, 2),
        (2, 1),
    ]
    await edition.refresh_from_db()
    assert edition.pages == 7
    await edition.delete()
    assert await first.editions.all().count() == 0


@pytest.mark.asyncio
async def test_the_relation_is_followed_both_ways(relation_key_db):
    await create_books()

    assert await RelationKeyEdition.objects.filter(book__title="first").order_by("number").values_list(
        "number", flat=True
    ) == [1, 2]
    assert await RelationKeyBook.objects.annotate(count=Count("editions")).order_by("id").values_list(
        "id", "count"
    ) == [(1, 2), (2, 1)]
    editions = await RelationKeyEdition.objects.select_related("book").order_by("book_id", "number")
    assert [(edition.book.title, edition.number) for edition in editions] == [
        ("first", 1),
        ("first", 2),
        ("second", 1),
    ]


@pytest.mark.asyncio
async def test_a_component_relation_to_a_composite_key_and_by_its_own_name(relation_key_db):
    first, second = await create_books()
    edition = await RelationKeyEdition.objects.get(pk=(1, 2))
    await RelationKeyNote.objects.create(edition=edition, position=1, text="note")
    await RelationKeyShelfSlot.objects.create(book=second, slot=4)

    note = await RelationKeyNote.objects.get(pk=(1, 2, 1))
    assert note.text == "note"
    assert (await note.edition).pk == (1, 2)
    assert await RelationKeyNote.objects.filter(edition__book__title="first").values_list("text", flat=True) == [
        "note"
    ]
    slot = await RelationKeyShelfSlot.objects.get(pk=(4, 2))
    assert (await slot.book).pk == second.pk
    assert await first.slots.all().count() == 0


@pytest.mark.asyncio
async def test_a_relation_to_the_key_from_a_model_set_up_before_its_target(relation_key_db):
    # RelationKeyChapter is set up before RelationKeyEdition, whose key holds its relation's column.
    assert set(RelationKeyChapter._meta.fields_db_projection) == {"id", "edition_book_id", "edition_number"}
    await create_books()
    edition = await RelationKeyEdition.objects.get(pk=(1, 2))
    await RelationKeyChapter.objects.create(id=1, edition=edition)
    await RelationKeyChapter.objects.create(id=2, edition_book_id=1, edition_number=2)
    assert await RelationKeyChapter.objects.filter(edition=edition).count() == 2
    assert await RelationKeyChapter.objects.filter(edition__pk=(1, 2)).count() == 2
    assert await edition.chapters.all().count() == 2
    assert (await (await RelationKeyChapter.objects.get(id=1)).edition).pk == (1, 2)
    await edition.delete()
    assert await RelationKeyChapter.objects.count() == 0


@pytest.mark.database_independent
def test_a_component_relation_must_hold_a_value():
    with pytest.raises(ConfigurationError, match="nullable relation"):

        class NullableComponent(Model):
            book = fields.ForeignKeyField("models.RelationKeyBook", null=True)
            number = fields.IntField()
            pk = CompositePrimaryKey("book_id", "number")

    with pytest.raises(ConfigurationError, match="many-to-many"):

        class ManyToManyComponent(Model):
            books = fields.ManyToManyField("models.RelationKeyBook")
            number = fields.IntField()
            pk = CompositePrimaryKey("books", "number")


@pytest.mark.database_independent
def test_a_composite_key_is_the_only_primary_key_and_holds_values():
    with pytest.raises(ConfigurationError, match="'b' on model NullableScalarComponent is nullable"):

        class NullableScalarComponent(Model):
            a = fields.IntField()
            b = fields.IntField(null=True)
            pk = CompositePrimaryKey("a", "b")

    with pytest.raises(ConfigurationError, match="primary key field 'id' and a CompositePrimaryKey"):

        class TwoPrimaryKeys(Model):
            id = fields.IntField(primary_key=True)
            a = fields.IntField()
            b = fields.IntField()
            pk = CompositePrimaryKey("a", "b")
