"""A model's truth value does not decide whether a forward relation exists."""

import sys
from contextlib import nullcontext
from types import ModuleType

import pytest
import pytest_asyncio

from hare import Model, Transactions, fields
from hare.contrib.test import hare_test_context, requires_features
from hare.core.hare_context import HareContext
from hare.exceptions import TransactionManagementError
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(params=(False, True), ids=("single-key", "composite-key"))
async def relation_models(request, monkeypatch, truth_method):
    class Author(Model):
        number = fields.IntField(primary_key=not request.param, generated=False)
        if request.param:
            revision = fields.IntField()
            pk = fields.CompositePrimaryKey("number", "revision")

    class Book(Model):
        id = fields.IntField(primary_key=True, generated=False)
        author = fields.ForeignKeyField("models.Author", null=True, related_name="books", db_constraint=False)
        if DatabaseUnderTest.get_dialect().features.supports_unique_constraints:
            reviewer = fields.OneToOneField("models.Author", null=True, related_name=False, db_constraint=False)

    monkeypatch.setattr(Author, truth_method, lambda self: False, raising=False)
    module = ModuleType("tests._relation_instance_presence")
    module.__models__ = [Author, Book]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    async with hare_test_context([module.__name__], DatabaseUnderTest.get_url()):
        author = await Author.objects.create(number=7, **({"revision": 2} if request.param else {}))
        assert not author
        yield author, Book


@pytest.fixture(params=("__bool__", "__len__"))
def truth_method(request):
    return request.param


@pytest.mark.asyncio
async def test_forward_assignment_keeps_the_key_of_a_falsy_model(relation_models):
    author, book_model = relation_models
    for relation_name in (
        HareContext.require_current().apps.get_binding(book_model).foreign_key_fields
        | HareContext.require_current().apps.get_binding(book_model).one_to_one_fields
    ):
        relation = HareContext.require_current().apps.get_binding(book_model).fields_map[relation_name]
        expected = tuple(getattr(author, name) for name in relation.to_field_names)
        for path in ("constructor", "setter", "update_from_dict"):
            if path == "constructor":
                book = book_model(id=1, **{relation_name: author})
            else:
                book = book_model(id=1)
                if path == "setter":
                    setattr(book, relation_name, author)
                else:
                    book.update_from_dict({relation_name: author})
            assert getattr(book, relation_name) is author
            assert tuple(getattr(book, name) for name in relation.source_fields) == expected
            setattr(book, relation_name, None)
            assert all(getattr(book, name) is None for name in relation.source_fields)


@pytest.mark.asyncio
async def test_prefetch_does_not_clear_the_key_of_a_falsy_model(relation_models):
    author, book_model = relation_models
    values = {}
    relation_names = (
        HareContext.require_current().apps.get_binding(book_model).foreign_key_fields
        | HareContext.require_current().apps.get_binding(book_model).one_to_one_fields
    )
    for relation_name in relation_names:
        relation = HareContext.require_current().apps.get_binding(book_model).fields_map[relation_name]
        values.update(
            (source, getattr(author, target))
            for source, target in zip(relation.source_fields, relation.to_field_names, strict=True)
        )
    await book_model.objects.create(id=1, **values)
    book = await book_model.objects.prefetch_related(*relation_names).get(id=1)
    assert all(getattr(book, name).pk == author.pk for name in relation_names)
    assert {name: getattr(book, name) for name in values} == values
    await book.save()
    assert await book_model.objects.get(id=1).values(*values) == values


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
@pytest.mark.parametrize("operation", ("add", "remove"))
@pytest.mark.parametrize("write_scope", ("internal-bulk", "ambient-bulk", "internal-save"))
async def test_failed_related_assignment_restores_keys_and_cached_parent(
    relation_models, monkeypatch, operation, write_scope
):
    original, book_model = relation_models
    author_model = type(original)
    replacement = await author_model.objects.create(
        number=8, **({"revision": 3} if original._binding.has_composite_primary_key else {})
    )
    await book_model.objects.create(id=1, author=original)
    book = await book_model.objects.select_related("author").get(id=1)
    cached = book.author
    relation = HareContext.require_current().apps.get_binding(book_model).fields_map["author"]
    before = tuple(getattr(book, name) for name in relation.source_fields)
    rejected_value = None if operation == "remove" else getattr(replacement, relation.to_field_names[-1])
    normal_setattr = book_model.__setattr__

    def reject_changed_key(obj, name, value):
        normal_setattr(obj, name, value)
        if obj is book and name == relation.source_fields[-1] and value == rejected_value:
            raise ValueError("rejected related key")

    monkeypatch.setattr(book_model, "__setattr__", reject_changed_key)
    parent = original if operation == "remove" else replacement
    manager = parent.books
    with pytest.raises(ValueError, match="rejected related key"):
        async with Transactions.atomic() if write_scope == "ambient-bulk" else nullcontext():
            await getattr(manager, operation)(book, bulk=write_scope != "internal-save")
    assert tuple(getattr(book, name) for name in relation.source_fields) == before
    assert book.__dict__.get("_author") is cached
    stored = await book_model.objects.get(id=1)
    assert tuple(getattr(stored, name) for name in relation.source_fields) == before


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
@pytest.mark.parametrize("boundary", ("after", "inside"))
async def test_deferred_related_bulk_uses_the_transaction_at_execution(relation_models, boundary):
    author, book_model = relation_models
    obj = book_model(id=1)
    if boundary == "after":
        async with Transactions.atomic():
            query = author.books.bulk_create([obj], returning=False)
        await query
    else:
        query = author.books.bulk_create([obj], returning=False)
        async with Transactions.atomic():
            await query
    relation = HareContext.require_current().apps.get_binding(book_model).fields_map["author"]
    stored = await book_model.objects.get(id=1)
    assert tuple(getattr(stored, name) for name in relation.source_fields) == tuple(
        getattr(author, name) for name in relation.to_field_names
    )


@pytest.mark.asyncio
@requires_features(supports_transactions=True)
@pytest.mark.parametrize("selection", ("client", "alias"))
async def test_deferred_related_bulk_retains_an_explicit_transaction(relation_models, selection):
    author, book_model = relation_models
    async with Transactions.atomic() as transaction:
        source = author.books.using(transaction if selection == "client" else transaction.connection_alias)
        query = source.bulk_create([book_model(id=1)], returning=False)
    with pytest.raises(TransactionManagementError):
        await query
    assert not await book_model.objects.filter(id=1).exists()
