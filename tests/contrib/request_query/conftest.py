import datetime
import gc
import os
from dataclasses import dataclass
from weakref import WeakSet

import pytest
import pytest_asyncio

from hare.contrib.request_query import RequestQuery
from hare.contrib.test.isolated_contexts import hare_test_context
from tests.contrib.request_query.models import (
    Author,
    Book,
    BookStatus,
    LineReturn,
    OrderLine,
    Profile,
    Refund,
    Tag,
    Visit,
)


def _get_request_query_classes() -> list[type]:
    classes: list[type] = []
    pending: list[type] = [RequestQuery]
    while pending:
        request_query_class = pending.pop()
        classes.append(request_query_class)
        pending.extend(request_query_class.__subclasses__())
    return classes


@pytest.fixture(autouse=True)
def _forget_test_request_queries():
    """A request query class a test declares - some wrong on purpose - is gone once the test ends:
    ``RequestQuery.check_declarations()`` checks every live subclass, and a class held only by
    reference cycles would otherwise outlive its test until a garbage collection. Collected only
    after a test that declared one - a full collection takes a tenth of a second."""
    classes_before = WeakSet(_get_request_query_classes())
    yield
    if any(request_query_class not in classes_before for request_query_class in _get_request_query_classes()):
        gc.collect()


@dataclass
class Library:
    anna: Author
    boris: Author
    clara: Author
    fantasy: Tag
    history: Tag
    books: dict[str, Book]


@pytest_asyncio.fixture
async def db_request_query():
    async with hare_test_context(
        modules=["tests.contrib.request_query.models"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture
async def library(db_request_query) -> Library:
    """Three authors, two tags, five books, order lines with a composite key, returns of lines,
    refunds of returns and visits of books - rows without a primary key."""
    anna = await Author.objects.create(id=1, name="Anna", rating=5, score=90)
    boris = await Author.objects.create(id=2, name="Boris", rating=3, score=50)
    clara = await Author.objects.create(id=3, name="Clara", rating=None, score=10)
    await Profile.objects.create(id=1, author=anna, city="Moscow")
    await Profile.objects.create(id=2, author=boris, city="Kazan")
    fantasy = await Tag.objects.create(id=1, name="fantasy")
    history = await Tag.objects.create(id=2, name="history")
    moment = datetime.datetime(2026, 3, 1, 12, tzinfo=datetime.UTC)
    books = {}
    for book_id, title, author, status, tags, days in (
        (1, "Alpha", anna, BookStatus.PUBLISHED, [fantasy], 0),
        (2, "Beta", anna, BookStatus.DRAFT, [history], 30),
        (3, "Gamma", boris, BookStatus.PUBLISHED, [fantasy, history], 60),
        (4, "Delta", boris, BookStatus.PUBLISHED, [], 400),
        (5, "Epsilon", clara, BookStatus.DRAFT, [fantasy], 10),
    ):
        book = await Book.objects.create(
            id=book_id,
            title=title,
            author=author,
            status=status,
            published_at=moment - datetime.timedelta(days=days),
        )
        for tag in tags:
            await book.tags.add(tag)
        books[title] = book
    for order_id, book_title in ((1, "Alpha"), (2, "Beta"), (3, "Gamma")):
        for line_no in (1, 2):
            await OrderLine.objects.create(
                order_id=order_id, line_no=line_no, book=books[book_title], quantity=line_no
            )
    first_return = await LineReturn.objects.create(id=1, line=await OrderLine.objects.get(pk=(2, 1)), reason="damaged")
    await LineReturn.objects.create(id=2, line=await OrderLine.objects.get(pk=(3, 2)), reason="late")
    await Refund.objects.create(id=1, line_return=first_return, amount=100)
    for visitor, book_title in (("vera", "Alpha"), ("ivan", "Alpha"), ("vera", "Gamma")):
        await Visit.objects.create(book=books[book_title], visitor=visitor)
    return Library(anna=anna, boris=boris, clara=clara, fantasy=fantasy, history=history, books=books)
