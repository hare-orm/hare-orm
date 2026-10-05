import pytest
import pytest_asyncio

from hare.exceptions import (
    QueryError,
)
from hare.query.expressions import Case, F, Q, RawSQL, Value, When, Window
from hare.query.functions import Count, Sum
from hare.query.functions.window import Lag, Rank, RowNumber
from tests.testmodels import Author, Book


@pytest_asyncio.fixture
async def library(db):
    first_author = await Author.objects.create(name="First")
    second_author = await Author.objects.create(name="Second")
    third_author = await Author.objects.create(name="Third")
    await Book.objects.create(name="A", author=first_author, rating=1.0)
    await Book.objects.create(name="B", author=first_author, rating=2.0)
    await Book.objects.create(name="C", author=second_author, rating=5.0)
    return first_author, second_author, third_author


def position_in_author() -> Window:
    return Window(RowNumber(), partition_by=["author_id"], order_by=["-rating", "id"])


@pytest.mark.asyncio
async def test_window_filter_keeps_the_group_by_of_values_list(library):
    """The inner query of a filter on a window over an aggregate used to lose its GROUP BY."""
    _first_author, second_author, _third_author = library
    rows = (
        await Author.objects.annotate(total=Sum("books__rating"))
        .annotate(rank=Window(Rank(), order_by=[F("total").desc(nulls_last=True)]))
        .filter(rank=1)
        .values_list("id", "total")
    )
    assert rows == [(second_author.id, 5.0)]


@pytest.mark.asyncio
async def test_window_filter_keeps_the_group_by_of_values(library):
    first_author, second_author, _third_author = library
    rows = (
        await Author.objects.annotate(book_count=Count("books"))
        .annotate(position=Window(RowNumber(), order_by=["-book_count", "id"]))
        .filter(position__lte=2)
        .order_by("id")
        .values("id", "book_count")
    )
    assert rows == [{"id": first_author.id, "book_count": 2}, {"id": second_author.id, "book_count": 1}]


@pytest.mark.asyncio
async def test_window_filter_keeps_the_with_clause(library):
    names = (
        await Book.objects.all()
        .with_cte("rated", Book.objects.filter(rating__gte=2).values("id"))
        .filter(id__in=RawSQL('SELECT "id" FROM "rated"'))
        .annotate(position=Window(RowNumber(), order_by=["-rating"]))
        .filter(position=1)
        .values_list("name", flat=True)
    )
    assert names == ["C"]


@pytest.mark.asyncio
async def test_window_filter_compared_with_a_field(library):
    names = (
        await Book.objects.annotate(previous=Window(Lag("rating", 1, 0.0), order_by=["id"]))
        .filter(previous=F("rating") - 1)
        .order_by("id")
        .values_list("name", flat=True)
    )
    assert names == ["A", "B"]


@pytest.mark.asyncio
async def test_field_filter_compared_with_a_window(library):
    names = (
        await Book.objects.annotate(previous=Window(Lag("rating"), order_by=["id"]))
        .filter(rating__gt=F("previous"))
        .order_by("id")
        .values_list("name", flat=True)
    )
    assert names == ["B", "C"]


@pytest.mark.asyncio
async def test_field_filter_compared_with_a_window_raises_for_model_instances(library):
    with pytest.raises(QueryError, match="window function"):
        await Book.objects.annotate(previous=Window(Lag("rating"), order_by=["id"])).filter(rating__gt=F("previous"))


@pytest.mark.asyncio
async def test_window_filter_in_or_and_exclude(library):
    either = (
        await Book.objects.annotate(position=position_in_author())
        .filter(Q(position=2) | Q(name="C"))
        .order_by("id")
        .values_list("name", flat=True)
    )
    assert either == ["A", "C"]
    excluded = (
        await Book.objects.annotate(position=position_in_author())
        .exclude(position=1)
        .order_by("id")
        .values_list("name", flat=True)
    )
    assert excluded == ["A"]


@pytest.mark.asyncio
async def test_window_filter_applies_before_limit_and_offset(library):
    queryset = Book.objects.annotate(position=position_in_author()).filter(position=1).order_by("-id")
    assert await queryset.limit(1).values_list("name", flat=True) == ["C"]
    assert await queryset.offset(1).values_list("name", flat=True) == ["B"]


@pytest.mark.asyncio
async def test_window_filter_applies_before_distinct(library):
    first_author, second_author, _third_author = library
    author_ids = (
        await Book.objects.annotate(position=Window(RowNumber(), order_by=["id"]))
        .filter(position__lte=3)
        .order_by("author_id")
        .distinct()
        .values_list("author_id", flat=True)
    )
    assert author_ids == [first_author.id, second_author.id]


@pytest.mark.asyncio
async def test_window_filter_ordered_by_an_unselected_window(library):
    names = (
        await Book.objects.annotate(position=Window(RowNumber(), order_by=["-rating"]))
        .filter(position__lte=2)
        .order_by("position")
        .values_list("name", flat=True)
    )
    assert names == ["C", "B"]


@pytest.mark.asyncio
async def test_window_filter_applies_before_the_cursor(library):
    first_book = await Book.objects.get(name="A")
    rows = (
        await Book.objects.annotate(position=Window(RowNumber(), order_by=["id"]))
        .filter(position__gte=1)
        .order_by("id")
        .after_cursor(first_book.id)
        .values_list("name", "position")
    )
    assert rows == [("B", 2), ("C", 3)]


@pytest.mark.asyncio
async def test_window_filter_with_select_for_update_raises(library):
    if not Book._meta.connection.features.supports_select_for_update:
        pytest.skip("select_for_update() is not supported by this backend")
    with pytest.raises(QueryError, match="select_for_update"):
        await (
            Book.objects.annotate(position=Window(RowNumber(), order_by=["id"]))
            .filter(position=1)
            .select_for_update()
            .values_list("id", flat=True)
        )


@pytest.mark.asyncio
async def test_window_in_a_case_condition(library):
    rows = (
        await Book.objects.annotate(position=Window(RowNumber(), order_by=["id"]))
        .annotate(bucket=Case(When(position__lte=2, then=Value("top")), default=Value("rest")))
        .order_by("id")
        .values_list("name", "bucket")
    )
    assert rows == [("A", "top"), ("B", "top"), ("C", "rest")]


@pytest.mark.asyncio
async def test_aggregate_over_a_window_raises(library):
    with pytest.raises(QueryError, match="aggregate function over a window function"):
        await Book.objects.annotate(position=Window(RowNumber(), order_by=["id"])).aggregate(total=Sum("position"))


@pytest.mark.asyncio
async def test_update_to_a_window_raises(library):
    with pytest.raises(QueryError, match="can't be updated to a window function"):
        await Book.objects.all().update(rating=Window(RowNumber(), order_by=["id"]))
