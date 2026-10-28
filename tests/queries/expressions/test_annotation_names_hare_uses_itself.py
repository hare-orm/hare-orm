"""A query's own annotations named as hare once named the annotations and columns it adds itself - hare's names
hold spaces now, which no annotation name can, so the user's annotation is read as given."""

import pytest

from hare import Prefetch
from hare.query.expressions import F, Value, Window
from hare.query.functions import Count
from hare.query.functions.window import RowNumber
from tests.shared_models.testmodels import Author, Book


async def create_books() -> Author:
    author = await Author.objects.create(name="First")
    await Book.objects.create(id=1, name="A", author=author, rating=1.0)
    await Book.objects.create(id=2, name="B", author=author, rating=2.0)
    return author


@pytest.mark.asyncio
async def test_an_iterator_keeps_an_annotation_named_like_its_cursor(db):
    """iterator() dropped every annotation named like the ones it orders its pages by."""
    await create_books()
    query = Book.objects.annotate(hare_iterator_cursor_note=Value("kept")).order_by("id")
    rows = [row async for row in query.iterator(chunk_size=1)]
    assert [(row.id, row.hare_iterator_cursor_note) for row in rows] == [(1, "kept"), (2, "kept")]


@pytest.mark.asyncio
async def test_a_sliced_prefetch_keeps_an_annotation_named_like_its_row_number(db):
    """A sliced Prefetch numbered the rows under the annotation's name, and its filter read the number."""
    author = await create_books()
    books = Book.objects.annotate(hare_prefetch_row_number=Value(7)).filter(hare_prefetch_row_number=7).order_by("id")
    row = await Author.objects.prefetch_related(Prefetch("books", books[:1])).get(pk=author.pk)
    assert [(book.id, book.hare_prefetch_row_number) for book in row.books] == [(1, 7)]


@pytest.mark.asyncio
async def test_a_window_filter_keeps_an_annotation_named_like_its_column(db):
    """A filter by a window function selected it under the annotation's name - the column was ambiguous."""
    await create_books()
    query = (
        Book.objects.annotate(hare_window_filter_1=Value(7), number=Window(RowNumber(), order_by=["id"]))
        .filter(number=1)
        .order_by()
    )
    assert await query.values("id", "hare_window_filter_1") == [{"id": 1, "hare_window_filter_1": 7}]


@pytest.mark.asyncio
async def test_a_union_keeps_annotations_named_like_its_model_columns(db):
    """A union selected the app and the model of each row under these names, in place of the annotations."""
    await create_books()
    query = Book.objects.filter(id=1).annotate(hare_app=Value("user app"), hare_model=Value("user model"))
    rows = await query.union(query, all=True)
    assert [(row.hare_app, row.hare_model) for row in rows] == [("user app", "user model")] * 2


@pytest.mark.asyncio
async def test_distinct_keeps_an_annotation_named_like_its_ordering_column(db):
    """DISTINCT selected the ordered expression under the annotation's name - the rows came in its order."""
    await create_books()
    rows = await Book.objects.annotate(hare_distinct_ordering_0=Value(7)).order_by(F("rating") * -1).distinct()
    assert [(row.id, row.hare_distinct_ordering_0) for row in rows] == [(2, 7), (1, 7)]


@pytest.mark.asyncio
async def test_aggregate_keeps_a_metric_named_like_the_metrics_hare_names(db):
    """aggregate() cut the prefix of its own metric names off the user's name."""
    await create_books()
    assert await Book.objects.aggregate(hare_metric_total=Count("id")) == {"hare_metric_total": 2}
