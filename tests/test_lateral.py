"""Lateral: a values() queryset joined LATERAL under a name - several columns of a subquery run for
each row, read in values(), F(), filters and order_by(), one row or several per outer row - and the
uses and databases that refuse it."""

from __future__ import annotations

import pytest

from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.query.expressions import F, FilteredRelation, Lateral, OuterReference, Q
from hare.query.functions import Count, Sum, Upper
from tests.testmodels import Author, Book


async def create_library() -> None:
    tolkien = await Author.objects.create(id=1, name="Tolkien")
    pratchett = await Author.objects.create(id=2, name="Pratchett")
    await Author.objects.create(id=3, name="Nobody")
    for book_id, author, name, rating in [
        (1, tolkien, "Hobbit", 4.5),
        (2, tolkien, "Silmarillion", 3.5),
        (3, tolkien, "Rings", 5.0),
        (4, pratchett, "Mort", 4.0),
        (5, pratchett, "Guards", 4.8),
    ]:
        await Book.objects.create(id=book_id, author=author, name=name, rating=rating)


def best_book(count: int = 1) -> Lateral:
    return Lateral(
        Book.objects.filter(author=OuterReference("pk")).order_by("-rating", "id").values("name", "rating")[:count]
    )


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
async def test_several_columns_of_one_row(db):
    await create_library()
    rows = await Author.objects.alias(best=best_book()).values("name", "best__name", "best__rating").order_by("id")
    assert rows == [
        {"name": "Tolkien", "best__name": "Rings", "best__rating": 5.0},
        {"name": "Pratchett", "best__name": "Guards", "best__rating": 4.8},
        {"name": "Nobody", "best__name": None, "best__rating": None},
    ]


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
async def test_annotate_is_an_alias_read_through_f(db):
    await create_library()
    authors = (
        await Author.objects.annotate(best=best_book())
        .annotate(best_name=F("best__name"), loud=Upper("best__name"), doubled=F("best__rating") * 2)
        .order_by("id")
    )
    assert [(author.name, author.best_name, author.loud, author.doubled) for author in authors] == [
        ("Tolkien", "Rings", "RINGS", 10.0),
        ("Pratchett", "Guards", "GUARDS", 9.6),
        ("Nobody", None, None, None),
    ]
    assert not hasattr(authors[0], "best")


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
async def test_filters_and_ordering_on_its_columns(db):
    await create_library()
    queryset = Author.objects.alias(best=best_book())
    assert await queryset.filter(best__rating__gte=4.9).values_list("name", flat=True) == ["Tolkien"]
    assert await queryset.filter(best__name="Guards").values_list("name", flat=True) == ["Pratchett"]
    assert await queryset.filter(best__name__isnull=True).values_list("name", flat=True) == ["Nobody"]
    assert await queryset.exclude(best__name__startswith="R").order_by("id").values_list("name", flat=True) == [
        "Pratchett",
        "Nobody",
    ]
    assert await queryset.filter(Q(best__rating__lt=4.9) | Q(name="Nobody")).order_by("id").values_list(
        "name", flat=True
    ) == ["Pratchett", "Nobody"]
    ordered = (
        await queryset.filter(best__rating__isnull=False).order_by("-best__rating").values_list("name", flat=True)
    )
    assert ordered == ["Tolkien", "Pratchett"]


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
async def test_several_rows_for_each_row(db):
    await create_library()
    rows = (
        await Author.objects.alias(top=best_book(2))
        .filter(top__name__isnull=False)
        .values_list("name", "top__name")
        .order_by("id", "-top__rating")
    )
    assert rows == [("Tolkien", "Rings"), ("Tolkien", "Hobbit"), ("Pratchett", "Guards"), ("Pratchett", "Mort")]


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
async def test_an_aggregate_in_the_subquery_and_a_second_lateral(db):
    await create_library()
    stats = Lateral(
        Book.objects.filter(author=OuterReference("pk")).values("author").annotate(books=Count("id")).values("books")
    )
    rows = (
        await Author.objects.alias(stats=stats, best=best_book())
        .values("name", "stats__books", "best__name")
        .order_by("id")
    )
    assert rows == [
        {"name": "Tolkien", "stats__books": 3, "best__name": "Rings"},
        {"name": "Pratchett", "stats__books": 2, "best__name": "Guards"},
        {"name": "Nobody", "stats__books": None, "best__name": None},
    ]


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
async def test_an_aggregate_over_its_column(db):
    await create_library()
    totals = await Author.objects.alias(best=best_book()).aggregate(
        total=Sum("best__rating"), count=Count("best__name")
    )
    assert totals == {"total": 9.8, "count": 2}
    grouped = (
        await Author.objects.alias(top=best_book(2))
        .values("name")
        .annotate(top_total=Sum("top__rating"))
        .order_by("name")
        .values_list("name", "top_total")
    )
    assert grouped == [("Nobody", None), ("Pratchett", 8.8), ("Tolkien", 9.5)]


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
async def test_beside_a_filtered_relation(db):
    await create_library()
    rows = (
        await Author.objects.alias(best=best_book(), low=FilteredRelation("books", condition=Q(books__rating__lt=4)))
        .values("name", "best__name", "low__name")
        .order_by("id")
    )
    assert rows == [
        {"name": "Tolkien", "best__name": "Rings", "low__name": "Silmarillion"},
        {"name": "Pratchett", "best__name": "Guards", "low__name": None},
        {"name": "Nobody", "best__name": None, "low__name": None},
    ]


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
async def test_a_column_it_doesnt_select_is_refused(db):
    await create_library()
    queryset = Author.objects.alias(best=best_book())
    with pytest.raises(FieldError, match="reads one of the columns its queryset selects \\(name, rating\\)"):
        await queryset.values("best__id")
    with pytest.raises(FieldError, match="a Lateral is read through its columns - 'best__<column>'"):
        queryset.values("best")
    with pytest.raises(FieldError, match="a Lateral is read through its columns"):
        queryset.values_list("best", flat=True)


@requires_features(supports_lateral=False)
@pytest.mark.asyncio
async def test_a_database_without_lateral_refuses_it(db):
    await create_library()
    async with capture_queries() as queries:
        with pytest.raises(UnSupportedError, match="needs a LATERAL subquery"):
            await Author.objects.alias(best=best_book()).values("best__name")
        with pytest.raises(UnSupportedError, match="needs a LATERAL subquery"):
            await Author.objects.alias(best=best_book()).filter(best__rating__gte=1).count()
    assert queries.count == 0


@pytest.mark.parametrize(
    "query",
    [
        lambda: Book.objects.all(),
        lambda: "books",
        lambda: Book.objects.filter(author=OuterReference("pk")),
    ],
    ids=["model rows", "a string", "a filtered queryset"],
)
def test_a_queryset_without_values_is_refused(query):
    with pytest.raises(QueryError, match="takes a values\\(\\)/values_list\\(\\) queryset"):
        Lateral(query())
