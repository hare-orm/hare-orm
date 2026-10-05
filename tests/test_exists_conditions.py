import pytest
import pytest_asyncio

from hare.query.expressions import Case, Exists, OuterReference, Q, Value, When
from hare.query.functions import Sum
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


def has_book_rated_at_least(rating: float) -> Exists:
    return Exists(Book.objects.filter(author_id=OuterReference("id"), rating__gte=rating))


@pytest.mark.asyncio
async def test_filter_and_exclude_by_exists(library):
    first_author, second_author, third_author = library
    assert await Author.objects.filter(has_book_rated_at_least(2)).order_by("id").values_list("id", flat=True) == [
        first_author.id,
        second_author.id,
    ]
    assert await Author.objects.filter(~has_book_rated_at_least(2)).values_list("id", flat=True) == [third_author.id]
    assert await Author.objects.exclude(has_book_rated_at_least(2)).values_list("id", flat=True) == [third_author.id]


@pytest.mark.asyncio
async def test_exists_combined_with_q(library):
    first_author, second_author, _third_author = library
    expected = [first_author.id, second_author.id]
    either = Q(has_book_rated_at_least(5)) | Q(name="First")
    assert await Author.objects.filter(either).order_by("id").values_list("id", flat=True) == expected
    assert (
        await Author.objects.filter(has_book_rated_at_least(5) | Q(name="First"))
        .order_by("id")
        .values_list("id", flat=True)
        == expected
    )
    assert (
        await Author.objects.filter(Q(name="First") | has_book_rated_at_least(5))
        .order_by("id")
        .values_list("id", flat=True)
        == expected
    )
    both = has_book_rated_at_least(1) & has_book_rated_at_least(2)
    assert await Author.objects.filter(both).order_by("id").values_list("id", flat=True) == [
        first_author.id,
        second_author.id,
    ]
    assert await Author.objects.filter(~Q(has_book_rated_at_least(1))).count() == 1


@pytest.mark.asyncio
async def test_exists_in_when(library):
    rows = (
        await Author.objects.annotate(
            level=Case(When(has_book_rated_at_least(5), then=Value("high")), default=Value("low"))
        )
        .order_by("id")
        .values_list("level", flat=True)
    )
    assert rows == ["low", "high", "low"]


@pytest.mark.asyncio
async def test_exists_filter_in_count_update_and_delete(library):
    first_author, second_author, third_author = library
    assert await Author.objects.filter(has_book_rated_at_least(1)).count() == 2
    assert await Author.objects.filter(has_book_rated_at_least(5)).update(name="Top") == 1
    assert (await Author.objects.get(id=second_author.id)).name == "Top"
    assert await Author.objects.filter(~has_book_rated_at_least(1)).delete() == 1
    assert await Author.objects.all().order_by("id").values_list("id", flat=True) == [
        first_author.id,
        second_author.id,
    ]
    assert third_author.id not in await Author.objects.all().values_list("id", flat=True)


@pytest.mark.asyncio
async def test_exists_over_a_values_query(library):
    _first_author, second_author, _third_author = library
    rich_author_ids = (
        Book.objects.filter(author_id=OuterReference("id"))
        .group_by("author_id")
        .annotate(total=Sum("rating"))
        .filter(total__gt=4)
        .values("author_id")
    )
    rows = (
        await Author.objects.annotate(is_rich=Exists(rich_author_ids)).order_by("id").values_list("is_rich", flat=True)
    )
    assert rows == [False, True, False]
    assert await Author.objects.filter(Exists(rich_author_ids)).values_list("id", flat=True) == [second_author.id]
    any_book = Exists(Book.objects.filter(author_id=OuterReference("id")).values_list("id", flat=True))
    assert await Author.objects.filter(any_book).count() == 2
