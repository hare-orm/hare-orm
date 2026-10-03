import os

import pytest
import pytest_asyncio

from hare.exceptions import (
    QueryError,
)
from hare.query.expressions import Exists, OuterRef, Subquery
from tests.testmodels import Author, Book
from tests.utils.multi_database_context import MultiDatabaseTestContext


@pytest_asyncio.fixture(scope="function")
async def two_databases():
    """Two genuinely separate physical connections ("models"/"events"), both schema'd with the
    full tests.testmodels module - mirrors tests/test_two_databases.py's own fixture. Author/Book
    declare no Meta.app, so they default to app "models"; .using(second_db) below is what
    forces a Book queryset onto the OTHER, "events" connection explicitly."""
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    async with MultiDatabaseTestContext.open(
        db_url,
        ["models", "events"],
        apps={
            "models": {"models": ["tests.testmodels"], "default_connection": "models"},
            "events": {"models": ["tests.testmodels"], "default_connection": "events"},
        },
    ) as ctx:
        await ctx.generate_schemas()

        db = ctx.connections.get("models")
        second_db = ctx.connections.get("events")

        yield db, second_db


@pytest.mark.asyncio
async def test_exists_using_db_different_from_outer_raises(two_databases):
    """Regression test: Exists(...) wrapping a queryset explicitly pinned (.using()) to a
    DIFFERENT physical connection than the outer query used to silently ignore that pin and run
    the whole compiled SQL text (including the wrapped SELECT) against the OUTER connection only
    - never erroring, just quietly matching nothing whenever the two connections' data actually
    differs. Must now raise ConfigurationError at query-build time instead."""
    db, second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    with pytest.raises(QueryError, match="different database connection"):
        await (
            Author.objects.all()
            .annotate(has_book=Exists(Book.objects.filter(author_id=OuterRef("id")).using(second_db)))
            .filter(has_book=True)
        )


@pytest.mark.asyncio
async def test_subquery_using_db_different_from_outer_raises(two_databases):
    """Same cross-connection guard, for a bare Subquery(...) annotation rather than Exists(...)."""
    db, second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    with pytest.raises(QueryError, match="different database connection"):
        await Author.objects.all().annotate(
            book_id=Subquery(Book.objects.filter(author_id=OuterRef("id")).using(second_db).values("id"))
        )


@pytest.mark.asyncio
async def test_exists_without_explicit_using_db_still_works(two_databases):
    """No explicit .using() on the wrapped queryset - it resolves through the router to the
    SAME connection as the outer query (both Author and Book default to app "models") - this
    legitimate, un-pinned case must keep working exactly as before, even with a second,
    unrelated connection alive in the same context."""
    db, _second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    other = await Author.objects.using(db).create(name="A2")
    await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    results = (
        await Author.objects.all()
        .using(db)
        .annotate(has_book=Exists(Book.objects.filter(author_id=OuterRef("id"))))
        .filter(has_book=True)
    )
    assert [a.id for a in results] == [author.id]
    assert other.id not in [a.id for a in results]


@pytest.mark.asyncio
async def test_pinned_exists_is_refused_after_the_same_query_kept_a_plan(two_databases):
    """The same query run unpinned first keeps a statement plan; the pinned one must not run on
    that plan, which would skip the check of the wrapped queryset's connection."""
    db, second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    unpinned = await (
        Author.objects.all()
        .annotate(has_book=Exists(Book.objects.filter(author_id=OuterRef("id"))))
        .filter(has_book=True)
    )
    assert [found.id for found in unpinned] == [author.id]

    with pytest.raises(QueryError, match="different database connection"):
        await (
            Author.objects.all()
            .annotate(has_book=Exists(Book.objects.filter(author_id=OuterRef("id")).using(second_db)))
            .filter(has_book=True)
        )


@pytest.mark.asyncio
async def test_pinned_subquery_is_refused_after_the_same_query_kept_a_plan(two_databases):
    """Same for a Subquery(...) annotation."""
    db, second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    book = await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    unpinned = await Author.objects.all().annotate(
        book_id=Subquery(Book.objects.filter(author_id=OuterRef("id")).values("id"))
    )
    assert [found.book_id for found in unpinned] == [book.id]

    with pytest.raises(QueryError, match="different database connection"):
        await Author.objects.all().annotate(
            book_id=Subquery(Book.objects.filter(author_id=OuterRef("id")).using(second_db).values("id"))
        )
