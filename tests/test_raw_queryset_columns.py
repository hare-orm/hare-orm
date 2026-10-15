import pytest
import pytest_asyncio

from hare.exceptions import FieldError, QueryError
from tests.testmodels import Author, Book


@pytest_asyncio.fixture
async def library(db):
    first_author = await Author.objects.create(name="First")
    second_author = await Author.objects.create(name="Second")
    await Book.objects.create(name="A", author=first_author, rating=1.0)
    await Book.objects.create(name="B", author=first_author, rating=2.0)
    return first_author, second_author


@pytest.mark.asyncio
async def test_raw_extra_columns_become_annotations(library):
    book_table = Book._meta.db_table
    books = await Book.objects.raw(
        f'SELECT "b".*, "b"."rating" * 2 AS "doubled", \'x\' AS "tag" FROM "{book_table}" "b" ORDER BY "b"."id"'
    )
    assert [(book.name, book.doubled, book.tag) for book in books] == [("A", 2.0, "x"), ("B", 4.0, "x")]


@pytest.mark.asyncio
async def test_raw_aggregate_column_becomes_an_annotation(library):
    first_author, second_author = library
    author_table = Author._meta.db_table
    book_table = Book._meta.db_table
    authors = await Author.objects.raw(
        f'SELECT "a".*, COUNT("b"."id") AS "book_count" FROM "{author_table}" "a" '
        f'LEFT JOIN "{book_table}" "b" ON "b"."author_id" = "a"."id" GROUP BY "a"."id", "a"."name" ORDER BY "a"."id"'
    )
    assert [(author.id, author.name, author.book_count) for author in authors] == [
        (first_author.id, "First", 2),
        (second_author.id, "Second", 0),
    ]


@pytest.mark.asyncio
async def test_raw_without_the_primary_key_raises(library):
    with pytest.raises(FieldError, match="primary key"):
        await Book.objects.raw(f'SELECT "name" FROM "{Book._meta.db_table}"')


@pytest.mark.asyncio
async def test_raw_with_a_duplicate_column_raises(library):
    with pytest.raises(QueryError, match="more than once"):
        await Book.objects.raw(
            f'SELECT "b".*, "a"."id", "a"."name" FROM "{Book._meta.db_table}" "b" '
            f'JOIN "{Author._meta.db_table}" "a" ON "a"."id" = "b"."author_id"'
        )


@pytest.mark.asyncio
async def test_raw_with_no_rows_or_no_result_set(library):
    assert await Book.objects.raw(f'SELECT * FROM "{Book._meta.db_table}" WHERE "id" < 0') == []
