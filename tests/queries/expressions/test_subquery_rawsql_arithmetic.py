import datetime

import pytest
import pytest_asyncio

from hare.query.expressions import F, OuterReference, RawSQL, Subquery
from hare.query.expressions.expression_context import ExpressionContext
from tests.shared_models.testmodels import Author, Book, IntFields, TimeDeltaFields


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


def test_reused_subquery_keeps_each_outer_table_binding(db):
    declaration = best_rating_of_author("id")
    connection = Author._meta.connection
    first_context = ExpressionContext(
        model=Author,
        table=Author._meta.basetable.as_("first_author"),
        annotations={},
        dialect=connection.dialect,
        connection=connection,
    )
    second_context = ExpressionContext(
        model=Author,
        table=Author._meta.basetable.as_("second_author"),
        annotations={},
        dialect=connection.dialect,
        connection=connection,
    )
    first = declaration.get_result(first_context).term
    first_sql = first.get_sql(connection.dialect.sql_context)
    second = declaration.get_result(second_context).term
    second_sql = second.get_sql(connection.dialect.sql_context)
    assert "first_author" in first_sql and "second_author" not in first_sql
    assert "second_author" in second_sql and "first_author" not in second_sql
    assert first.get_sql(connection.dialect.sql_context) == first_sql
    assert first is not second
    assert declaration.get_nested_query() is None


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


@pytest.mark.asyncio
async def test_a_subquery_divides_integers_and_durations(db):
    """An integer quotient, a duration's sum and a duration's quotient of a subquery operand were built by the
    subquery's own operators - a query expression where an SQL term belongs."""
    await IntFields.objects.create(id=1, intnum=7)
    await TimeDeltaFields.objects.create(id=1, timedelta=datetime.timedelta(minutes=10))
    number = Subquery(IntFields.objects.filter(id=1).values("intnum"))
    duration = Subquery(TimeDeltaFields.objects.filter(id=1).values("timedelta"))

    assert await IntFields.objects.annotate(value=number / 2).values_list("value", flat=True) == [3]
    assert await TimeDeltaFields.objects.annotate(later=duration + F("timedelta"), half=duration / 2).values_list(
        "later", "half"
    ) == [(datetime.timedelta(minutes=20), datetime.timedelta(minutes=5))]
