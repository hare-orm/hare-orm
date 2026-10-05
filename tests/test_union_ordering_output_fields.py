import datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from hare.query.expressions import F, Value
from hare.query.functions import Count, Sum
from tests.testmodels import Author, Book, DateFields, DecimalFields


@pytest_asyncio.fixture
async def library(db):
    first_author = await Author.objects.create(name="First")
    second_author = await Author.objects.create(name="Second")
    third_author = await Author.objects.create(name="Third")
    await Book.objects.create(name="A", author=first_author, rating=1.0)
    await Book.objects.create(name="B", author=first_author, rating=2.0)
    await Book.objects.create(name="C", author=second_author, rating=5.0)
    return first_author, second_author, third_author


@pytest.mark.asyncio
async def test_union_of_joined_branches_ordered_by_a_field(library):
    union = Book.objects.filter(author__name="First").union(Book.objects.filter(author__name="Second")).order_by("-id")
    books = await union
    assert [book.name for book in books] == ["C", "B", "A"]


@pytest.mark.asyncio
async def test_union_of_aggregated_branches_ordered_by_annotation_and_field(library):
    first_author, second_author, third_author = library
    union = (
        Author.objects.filter(id=first_author.id)
        .annotate(book_count=Count("books"))
        .union(Author.objects.filter(id__gte=second_author.id).annotate(book_count=Count("books")))
        .order_by("-book_count", "id")
    )
    authors = await union
    assert [(author.id, author.book_count) for author in authors] == [
        (first_author.id, 2),
        (second_author.id, 1),
        (third_author.id, 0),
    ]
    total_union = (
        Author.objects.filter(id=first_author.id)
        .annotate(total=Sum("books__rating"))
        .union(Author.objects.filter(id__gte=second_author.id).annotate(total=Sum("books__rating")))
        .order_by("id")
        .limit(2)
    )
    assert [(author.id, author.total) for author in await total_union] == [
        (first_author.id, 3.0),
        (second_author.id, 5.0),
    ]


@pytest.mark.asyncio
async def test_union_null_literal_branch_keeps_the_other_branchs_decoding(db):
    await DateFields.objects.create(id=1, date=datetime.date(2020, 1, 5))
    await DateFields.objects.create(id=2, date=datetime.date(2021, 1, 1))
    date_first = (
        DateFields.objects.filter(id=1)
        .annotate(x=F("date"))
        .union(DateFields.objects.filter(id=2).annotate(x=Value(None)))
        .order_by("id")
    )
    assert [(row.id, row.x) for row in await date_first] == [(1, datetime.date(2020, 1, 5)), (2, None)]
    null_first = (
        DateFields.objects.filter(id=2)
        .annotate(x=Value(None))
        .union(DateFields.objects.filter(id=1).annotate(x=F("date")))
        .order_by("id")
    )
    assert [(row.id, row.x) for row in await null_first] == [(1, datetime.date(2020, 1, 5)), (2, None)]


@pytest.mark.asyncio
async def test_union_decodes_through_the_first_branchs_field(db):
    await DecimalFields.objects.create(id=1, decimal=Decimal("1.5"), decimal_nodec=1)
    await DecimalFields.objects.create(id=2, decimal=Decimal("2"), decimal_nodec=1)
    union = (
        DecimalFields.objects.filter(id=1)
        .annotate(x=F("decimal"))
        .union(DecimalFields.objects.filter(id=2).annotate(x=F("id")))
        .order_by("id")
    )
    rows = [(row.id, row.x) for row in await union]
    assert rows == [(1, Decimal("1.5")), (2, Decimal("2"))]
    assert all(type(value) is Decimal for _id, value in rows)
