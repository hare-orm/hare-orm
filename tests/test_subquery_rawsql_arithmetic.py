import pytest
import pytest_asyncio

from hare.query.expressions import F, OuterReference, RawSQL, Subquery
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


def best_rating_of_author(author_reference: str) -> Subquery:
    return Subquery(
        Book.objects.filter(author_id=OuterReference(author_reference)).order_by("-rating").limit(1).values("rating")
    )


@pytest.mark.asyncio
async def test_subquery_on_the_left_of_arithmetic_keeps_its_outer_reference(library):
    rows = (
        await Author.objects.annotate(best=best_rating_of_author("id") + 1)
        .order_by("id")
        .values_list("best", flat=True)
    )
    assert rows == [3.0, 6.0, None]


@pytest.mark.asyncio
async def test_subquery_on_the_right_of_arithmetic_keeps_its_outer_reference(library):
    rows = (
        await Author.objects.annotate(gap=10 - best_rating_of_author("id"))
        .order_by("id")
        .values_list("gap", flat=True)
    )
    assert rows == [8.0, 5.0, None]


@pytest.mark.asyncio
async def test_subquery_times_a_field(library):
    first_author, second_author, _third_author = library
    best_book_id = Subquery(
        Book.objects.filter(author_id=OuterReference("id")).order_by("-rating").limit(1).values("id")
    )
    book_ids = {book.name: book.id for book in await Book.objects.all()}
    rows = (
        await Author.objects.annotate(product=best_book_id * F("id")).order_by("id").values_list("product", flat=True)
    )
    assert rows == [book_ids["B"] * first_author.id, book_ids["C"] * second_author.id, None]


@pytest.mark.asyncio
async def test_filter_by_subquery_arithmetic(library):
    names = (
        await Book.objects.filter(rating__gte=best_rating_of_author("author_id") - 0.5)
        .order_by("id")
        .values_list("name", flat=True)
    )
    assert names == ["B", "C"]


@pytest.mark.asyncio
async def test_update_to_subquery_arithmetic(library):
    await Book.objects.filter(name="A").update(
        rating=Subquery(Book.objects.filter(author_id=OuterReference("author_id"), name="B").values("rating")) + 1
    )
    assert (await Book.objects.get(name="A")).rating == 3.0


@pytest.mark.asyncio
async def test_rawsql_operand_is_embedded_not_bound(library):
    rows = (
        await Book.objects.annotate(bumped=F("rating") + RawSQL("%s", [1]))
        .order_by("id")
        .values_list("bumped", flat=True)
    )
    assert rows == [2.0, 3.0, 6.0]
    doubled = (
        await Book.objects.annotate(doubled=RawSQL('"rating"', []) * 2)
        .order_by("id")
        .values_list("doubled", flat=True)
    )
    assert doubled == [2.0, 4.0, 10.0]


@pytest.mark.asyncio
async def test_update_to_rawsql(library):
    updated_count = await Book.objects.all().update(rating=RawSQL('"rating" + %s', [10]))
    assert updated_count == 3
    assert await Book.objects.all().order_by("id").values_list("rating", flat=True) == [11.0, 12.0, 15.0]
