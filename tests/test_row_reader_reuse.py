"""Row readers are compiled once per query shape and shared by every query of that shape - each
query still reads its own rows correctly, also when many run at once."""

import asyncio

import pytest

from hare.contrib.test.helpers import hare_test_context
from hare.query.plans.statement_plans import StatementPlans
from tests.testmodels import Author, Book, IntFields


def get_compiled_reader_count() -> int:
    return len(StatementPlans.hydrate_functions)


@pytest.mark.asyncio
async def test_repeated_get_reuses_its_reader(db):
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.get(intnum=1)
    reader_count = get_compiled_reader_count()
    await IntFields.objects.get(intnum=1)
    assert get_compiled_reader_count() == reader_count


@pytest.mark.asyncio
async def test_prefetch_related_reuses_its_readers(db):
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="b", author=author, rating=1.0)
    await Book.objects.all().prefetch_related("author")
    reader_count = get_compiled_reader_count()
    await Book.objects.all().prefetch_related("author")
    assert get_compiled_reader_count() == reader_count


@pytest.mark.asyncio
async def test_only_shape_reads_a_partial_instance(db):
    await IntFields.objects.create(intnum=5)
    full = await IntFields.objects.get(intnum=5)
    partial = await IntFields.objects.filter(intnum=5).only("intnum").get()
    assert full.intnum == partial.intnum == 5
    assert partial._partial and not full._partial


@pytest.mark.asyncio
async def test_models_read_their_own_rows(db):
    author = await Author.objects.create(name="a")
    book = await Book.objects.create(name="b", author=author, rating=1.0)
    assert (await Author.objects.get(pk=author.id)).name == "a"
    assert (await Book.objects.get(pk=book.id)).name == "b"


@pytest.mark.asyncio
async def test_concurrent_prefetch_related_calls_read_their_own_relations(db):
    author_a = await Author.objects.create(name="author-a")
    author_b = await Author.objects.create(name="author-b")
    book_a = await Book.objects.create(name="book-a", author=author_a, rating=1.0)
    book_b = await Book.objects.create(name="book-b", author=author_b, rating=2.0)

    results_a, results_b = await asyncio.gather(
        Book.objects.filter(id=book_a.id).prefetch_related("author"),
        Book.objects.filter(id=book_b.id).prefetch_related("author"),
    )
    assert results_a[0].author.name == "author-a"
    assert results_b[0].author.name == "author-b"


@pytest.mark.asyncio
async def test_concurrent_gets_read_their_own_rows(db):
    objs = [await IntFields.objects.create(intnum=number) for number in range(20)]
    results = await asyncio.gather(*[IntFields.objects.get(pk=obj.id) for obj in objs])
    for obj, result in zip(objs, results, strict=True):
        assert (result.id, result.intnum) == (obj.id, obj.intnum)


@pytest.mark.asyncio
async def test_a_context_never_reads_through_another_contexts_connection():
    async with hare_test_context(
        modules=["tests.testmodels"], db_url="sqlite://:memory:", app_label="models", connection_label="models"
    ):
        await Author.objects.create(name="from-a")
        assert (await Author.objects.get(pk=1)).name == "from-a"

    async with hare_test_context(
        modules=["tests.testmodels"], db_url="sqlite://:memory:", app_label="models", connection_label="models"
    ):
        await Author.objects.create(name="from-b")
        assert (await Author.objects.get(pk=1)).name == "from-b"
