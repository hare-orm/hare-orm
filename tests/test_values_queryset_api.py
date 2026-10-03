"""values()/values_list() as full querysets - every chainable and terminal method, checked against
the same operation applied before .values()/.values_list() and against a Python reference."""

from collections import Counter

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.exceptions import (
    ConfigurationError,
    DoesNotExist,
    FieldError,
    MultipleObjectsReturned,
    QueryError,
    UnSupportedError,
)
from hare.query.expressions import F, Q, RawSQL, Subquery, Window
from hare.query.functions import Avg, Count, Max, Min, Sum
from hare.query.functions.window import Rank as WindowRank, RowNumber as WindowRowNumber, Sum as WindowSum
from hare.transactions.transactions import Transactions
from tests.testmodels import Author, Book, Team

BOOKS = [
    ("alpha", "ann", 5.0, "sci"),
    ("beta", "ann", 3.0, "sci"),
    ("gamma", "ann", 3.0, None),
    ("delta", "bob", 4.0, "art"),
    ("eps", "bob", 1.0, "sci"),
    ("zeta", "bob", 3.0, "art"),
    ("eta", "bob", 2.0, "art"),
]


@pytest_asyncio.fixture
async def library(db):
    authors = {name: await Author.objects.create(name=name) for name in ("ann", "bob", "cid")}
    rows = []
    for name, author_name, rating, subject in BOOKS:
        book = await Book.objects.create(name=name, author=authors[author_name], rating=rating, subject=subject)
        rows.append(
            {
                "id": book.id,
                "name": name,
                "author_id": authors[author_name].id,
                "author_name": author_name,
                "rating": rating,
                "subject": subject,
            }
        )
    return {"authors": authors, "rows": rows}


# Each shape: how a queryset turns into a values query, and how a reference row turns into its output.
SHAPES = {
    "values": (
        lambda queryset: queryset.values("name", "rating"),
        lambda row: {"name": row["name"], "rating": row["rating"]},
    ),
    "values_renamed": (
        lambda queryset: queryset.values(title="name", score="rating"),
        lambda row: {"title": row["name"], "score": row["rating"]},
    ),
    "values_expression": (
        lambda queryset: queryset.values("name", doubled=F("rating") * 2),
        lambda row: {"name": row["name"], "doubled": row["rating"] * 2},
    ),
    "values_list": (lambda queryset: queryset.values_list("name", "rating"), lambda row: (row["name"], row["rating"])),
    "values_list_flat": (lambda queryset: queryset.values_list("name", flat=True), lambda row: row["name"]),
    "values_list_named": (
        lambda queryset: queryset.values_list("name", "rating", named=True),
        lambda row: (row["name"], row["rating"]),
    ),
    "values_list_expression": (
        lambda queryset: queryset.values_list("name", doubled=F("rating") * 2),
        lambda row: (row["name"], row["rating"] * 2),
    ),
}

# Each operation: applied to a queryset or a values query alike, and to the reference rows.
OPERATIONS = {
    "filter": (
        lambda query: query.filter(rating__gte=3).order_by("name"),
        lambda rows: sorted((row for row in rows if row["rating"] >= 3), key=lambda row: row["name"]),
    ),
    "filter_q": (
        lambda query: query.filter(Q(rating__lt=2) | Q(name="alpha")).order_by("name"),
        lambda rows: sorted(
            (row for row in rows if row["rating"] < 2 or row["name"] == "alpha"), key=lambda row: row["name"]
        ),
    ),
    "exclude": (
        lambda query: query.exclude(author__name="bob").order_by("name"),
        lambda rows: sorted((row for row in rows if row["author_name"] != "bob"), key=lambda row: row["name"]),
    ),
    "filter_related": (
        lambda query: query.filter(author__name="bob").order_by("-name"),
        lambda rows: sorted(
            (row for row in rows if row["author_name"] == "bob"), key=lambda row: row["name"], reverse=True
        ),
    ),
    "order_by": (
        lambda query: query.order_by("-rating", "name"),
        lambda rows: sorted(rows, key=lambda row: (-row["rating"], row["name"])),
    ),
    "order_by_unselected": (
        lambda query: query.order_by("-id"),
        lambda rows: sorted(rows, key=lambda row: -row["id"]),
    ),
    "slice": (
        lambda query: query.order_by("name")[1:4],
        lambda rows: sorted(rows, key=lambda row: row["name"])[1:4],
    ),
    "slice_composed": (
        lambda query: query.order_by("name")[1:][2:4],
        lambda rows: sorted(rows, key=lambda row: row["name"])[1:][2:4],
    ),
    "limit_offset": (
        lambda query: query.order_by("name").offset(2).limit(3),
        lambda rows: sorted(rows, key=lambda row: row["name"])[2:5],
    ),
    "all": (
        lambda query: query.order_by("name").all(),
        lambda rows: sorted(rows, key=lambda row: row["name"]),
    ),
    "none": (lambda query: query.none(), lambda rows: []),
    "filter_none": (lambda query: query.filter(name="nope").order_by("name"), lambda rows: []),
    "using": (
        lambda query: query.order_by("name").using(Book._meta.db),
        lambda rows: sorted(rows, key=lambda row: row["name"]),
    ),
    "after_cursor": (
        lambda query: query.order_by("name").after_cursor("delta"),
        lambda rows: sorted((row for row in rows if row["name"] > "delta"), key=lambda row: row["name"]),
    ),
    "before_cursor": (
        lambda query: query.order_by("name").before_cursor("delta").limit(2),
        lambda rows: sorted((row for row in rows if row["name"] < "delta"), key=lambda row: row["name"])[-2:],
    ),
    "alias": (
        lambda query: query.alias(half=F("rating") / 2).filter(half__gte=2).order_by("name"),
        lambda rows: sorted((row for row in rows if row["rating"] / 2 >= 2), key=lambda row: row["name"]),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape_name", SHAPES)
@pytest.mark.parametrize("operation_name", OPERATIONS)
async def test_chained_operation_matches_queryset_and_reference(library, shape_name, operation_name):
    make_values, make_output = SHAPES[shape_name]
    apply_operation, reference_operation = OPERATIONS[operation_name]

    chained = await apply_operation(make_values(Book.objects.all()))
    applied_first = await make_values(apply_operation(Book.objects.all()))
    expected = [make_output(row) for row in reference_operation(library["rows"])]

    assert chained == expected
    assert applied_first == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("shape_name", SHAPES)
async def test_chained_values_query_keeps_its_type_and_shape(library, shape_name):
    make_values, _make_output = SHAPES[shape_name]
    values_query = make_values(Book.objects.all())

    chained = values_query.filter(rating__gte=1).exclude(name="nope").order_by("name").distinct().all()

    assert type(chained) is type(values_query)
    rows = await chained
    reference = await values_query.order_by("name")
    assert rows == reference
    if shape_name == "values_list_named":
        assert rows[0].name == "alpha"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape_name", SHAPES)
async def test_terminal_methods_match_reference(library, shape_name):
    make_values, make_output = SHAPES[shape_name]
    rows = library["rows"]
    by_name = sorted(rows, key=lambda row: row["name"])
    values_query = make_values(Book.objects.all())

    assert await values_query.count() == len(rows)
    assert await values_query.filter(rating__gte=3).count() == len([row for row in rows if row["rating"] >= 3])
    assert await values_query.order_by("name")[2:5].count() == 3
    assert await values_query.order_by("name")[5:].count() == 2
    assert await values_query.none().count() == 0
    assert await values_query.exists() is True
    assert await values_query.filter(name="nope").exists() is False
    assert await values_query.order_by("name")[7:].exists() is False
    assert await values_query.order_by("name")[6:].exists() is True
    assert await values_query.none().exists() is False

    assert await values_query.order_by("name").first() == make_output(by_name[0])
    assert await values_query.order_by("name").last() == make_output(by_name[-1])
    by_id = sorted(rows, key=lambda row: row["id"])
    assert await values_query.first() == make_output(by_id[0])
    assert await values_query.last() == make_output(by_id[-1])
    assert await values_query.filter(name="nope").first() is None

    assert await values_query.get(name="delta") == make_output(next(row for row in rows if row["name"] == "delta"))
    assert await values_query.filter(author__name="bob").get(rating=1) == make_output(
        next(row for row in rows if row["name"] == "eps")
    )
    assert await values_query.get_or_none(name="nope") is None
    with pytest.raises(DoesNotExist):
        await values_query.get(name="nope")
    with pytest.raises(LookupError):
        await values_query.get(name="nope", exception=LookupError)
    with pytest.raises(MultipleObjectsReturned):
        await values_query.get(rating=3)
    with pytest.raises(MultipleObjectsReturned):
        await values_query.get_or_none(rating=3)

    assert await values_query.earliest("rating") == make_output(min(rows, key=lambda row: row["rating"]))
    assert await values_query.latest("rating") == make_output(max(rows, key=lambda row: row["rating"]))
    assert await values_query.filter(rating=3).earliest("name") == make_output(
        min((row for row in rows if row["rating"] == 3), key=lambda row: row["name"])
    )

    assert await values_query.order_by("name")[0] == make_output(by_name[0])
    assert await values_query.order_by("name")[3] == make_output(by_name[3])
    assert await values_query.order_by("name")[2:][1] == make_output(by_name[3])
    with pytest.raises(IndexError):
        await values_query.order_by("name")[7]
    with pytest.raises(QueryError):
        values_query.order_by("name")[-1]

    assert [row async for row in values_query.order_by("name")] == [make_output(row) for row in by_name]


@pytest.mark.asyncio
async def test_values_list_named_single_row_methods(library):
    row = await Book.objects.all().values_list("name", "rating", named=True).get(name="alpha")
    assert (row.name, row.rating) == ("alpha", 5.0)
    first = await Book.objects.all().values_list("name", named=True).order_by("-name").first()
    assert first.name == "zeta"


# ---------------------------------------------------------------------------
# Grouping: values(...).annotate(<aggregate>) groups by the selected fields, like Django.
# ---------------------------------------------------------------------------


def _group_books(rows, key_name):
    groups: dict = {}
    for row in rows:
        groups.setdefault(row[key_name], []).append(row)
    return groups


@pytest.mark.asyncio
async def test_values_annotate_groups_by_selected_fields(library):
    groups = _group_books(library["rows"], "author_id")
    expected = sorted(
        (
            {"author_id": author_id, "books": len(group), "total": sum(row["rating"] for row in group)}
            for author_id, group in groups.items()
        ),
        key=lambda row: row["author_id"],
    )

    grouped = Book.objects.all().values("author_id").annotate(books=Count("id"), total=Sum("rating"))
    assert await grouped.order_by("author_id") == expected
    # Annotated before values(), like Django, the aggregates are computed per book instead.
    assert await Book.objects.all().annotate(books=Count("id"), total=Sum("rating")).values(
        "author_id", "books", "total"
    ).order_by("author_id", "id") == [
        {"author_id": row["author_id"], "books": 1, "total": row["rating"]}
        for row in sorted(library["rows"], key=lambda row: (row["author_id"], row["id"]))
    ]
    assert await grouped.count() == len(groups)
    assert len(await grouped) == len(groups)
    assert await grouped.exists() is True
    assert await grouped.filter(books__gt=3).order_by("author_id") == [row for row in expected if row["books"] > 3]
    assert await grouped.filter(books__gt=3).count() == len([row for row in expected if row["books"] > 3])
    assert await grouped.filter(books__gt=10).exists() is False
    assert await grouped.order_by("-total") == sorted(expected, key=lambda row: -row["total"])
    assert await grouped.order_by("author_id")[1:] == expected[1:]
    assert await grouped.order_by("author_id")[1:].count() == len(expected) - 1
    assert await grouped.order_by("author_id")[0] == expected[0]
    assert await grouped.aggregate(most=Max("books"), groups=Count("author_id"), least=Min("total")) == {
        "most": max(row["books"] for row in expected),
        "groups": len(expected),
        "least": min(row["total"] for row in expected),
    }
    assert await grouped.order_by("author_id")[:1].aggregate(most=Max("books")) == {"most": expected[0]["books"]}


@pytest.mark.asyncio
async def test_values_list_annotate_groups_and_flat_keeps_one_column(library):
    groups = _group_books(library["rows"], "author_name")
    grouped = Book.objects.all().values_list("author__name").annotate(books=Count("id")).order_by("author__name")
    assert await grouped == sorted((name, len(group)) for name, group in groups.items())
    # Django's "which authors have more than three books" idiom - flat keeps its one column.
    prolific = (
        Book.objects.all().values_list("author__name", flat=True).annotate(books=Count("id")).filter(books__gt=3)
    )
    assert await prolific == [name for name, group in groups.items() if len(group) > 3]
    assert sorted(
        await Book.objects.all().values_list("author__name", flat=True).annotate(books=Count("id"))
    ) == sorted(groups)
    assert [author.name for author in await Author.objects.filter(name__in=prolific)] == ["bob"]


@pytest.mark.asyncio
async def test_grouped_first_last_order_by_group_key_when_unordered(library):
    grouped = Book.objects.all().values("subject").annotate(books=Count("id")).exclude(subject=None)
    assert await grouped.first() == {"subject": "art", "books": 3}
    assert await grouped.last() == {"subject": "sci", "books": 3}
    assert await grouped.order_by("-books", "subject").first() == {"subject": "art", "books": 3}


@pytest.mark.asyncio
async def test_explicit_group_by_chained_after_values(library):
    grouped = (
        Book.objects.all().values("author_id").group_by("author_id").annotate(best=Max("rating")).order_by("author_id")
    )
    groups = _group_books(library["rows"], "author_id")
    assert await grouped == [
        {"author_id": author_id, "best": max(row["rating"] for row in group)}
        for author_id, group in sorted(groups.items())
    ]
    assert await grouped.count() == len(groups)
    assert await grouped.aggregate(best_of_all=Max("best")) == {"best_of_all": 5.0}


@pytest.mark.asyncio
async def test_annotate_on_values_over_to_many_relation(library):
    per_author = Author.objects.all().values("name").annotate(book_count=Count("books")).order_by("name")
    counts = Counter(row["author_name"] for row in library["rows"])
    assert await per_author == [{"name": name, "book_count": counts.get(name, 0)} for name in ("ann", "bob", "cid")]
    assert await per_author.filter(book_count=0).values_list("name", flat=True) == ["cid"]


# ---------------------------------------------------------------------------
# .distinct() ordered by a field it doesn't select - the first row of each combination of the
# selected columns, in the ordering, deduplicated and sliced in SQL.
# ---------------------------------------------------------------------------


def _first_occurrences(values):
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


@pytest.mark.asyncio
async def test_distinct_ordered_by_unselected_field_keeps_first_occurrences(library):
    ordered_rows = sorted(library["rows"], key=lambda row: (-row["rating"], row["id"]))
    expected = _first_occurrences([row["subject"] for row in ordered_rows])

    subjects = Book.objects.all().order_by("-rating", "id").distinct().values_list("subject", flat=True)
    assert await subjects == expected
    assert await subjects[1:] == expected[1:]
    assert await subjects[:1] == expected[:1]
    assert await subjects[1:2] == expected[1:2]
    assert await subjects.count() == len(expected)
    assert await subjects[1:].count() == len(expected) - 1
    assert await subjects.exists() is True
    assert await subjects.first() == expected[0]
    assert await subjects[1] == expected[1]
    assert [subject async for subject in subjects] == expected
    assert [subject async for subject in subjects.iterator(chunk_size=1)] == expected
    # Chained after values() too.
    assert await Book.objects.all().values_list("subject", flat=True).order_by("-rating", "id").distinct() == expected
    dict_rows = await Book.objects.all().values("subject").distinct().order_by("-rating", "id")
    assert dict_rows == [{"subject": subject} for subject in expected]
    # As a subquery, sliced: the slice is taken over the deduplicated rows.
    top_subjects = subjects.exclude(subject=None)[:1]
    assert sorted(book.name for book in await Book.objects.filter(subject__in=top_subjects)) == sorted(
        row["name"] for row in library["rows"] if row["subject"] == expected[0]
    )


@pytest.mark.asyncio
async def test_distinct_over_selected_fields_counts_and_aggregates_distinct_rows(library):
    distinct_ratings = sorted({row["rating"] for row in library["rows"]})
    ratings = Book.objects.all().values_list("rating", flat=True).distinct()
    assert sorted(await ratings) == distinct_ratings
    assert await ratings.count() == len(distinct_ratings)
    assert await ratings.aggregate(total=Sum("rating"), average=Avg("rating")) == {
        "total": sum(distinct_ratings),
        "average": sum(distinct_ratings) / len(distinct_ratings),
    }
    assert await ratings.order_by("-rating")[:2].aggregate(total=Sum("rating")) == {
        "total": sum(distinct_ratings[-2:])
    }
    with pytest.raises(QueryError, match="doesn't select"):
        await ratings.aggregate(total=Sum("id"))


@pytest.mark.asyncio
async def test_distinct_first_occurrence_reuses_query_shape_cache_without_leaking_values(library):
    for threshold in (1, 3, 5, 2, 1):
        ordered_rows = sorted(
            (row for row in library["rows"] if row["rating"] >= threshold), key=lambda row: (-row["rating"], row["id"])
        )
        expected = _first_occurrences([row["subject"] for row in ordered_rows])
        subjects = (
            Book.objects.filter(rating__gte=threshold)
            .order_by("-rating", "id")
            .distinct()
            .values_list("subject", flat=True)
        )
        assert await subjects == expected
        assert await subjects[1:] == expected[1:]


# ---------------------------------------------------------------------------
# aggregate()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregate_over_source_rows_can_read_any_field(library):
    rows = library["rows"]
    assert await Book.objects.all().values("name").aggregate(total=Sum("rating"), books=Count("id")) == {
        "total": sum(row["rating"] for row in rows),
        "books": len(rows),
    }
    assert await Book.objects.filter(author__name="ann").values_list("id", flat=True).aggregate(
        best=Max("rating")
    ) == {"best": 5.0}
    assert await Book.objects.all().values(doubled=F("rating") * 2).aggregate(total=Sum("doubled")) == {
        "total": sum(row["rating"] * 2 for row in rows)
    }


@pytest.mark.asyncio
async def test_aggregate_over_sliced_rows_reads_selected_columns(library):
    by_name = sorted(library["rows"], key=lambda row: row["name"])
    sliced = Book.objects.all().values("name", score="rating").order_by("name")[:3]
    assert await sliced.aggregate(total=Sum("score"), same=Sum("rating"), books=Count("name")) == {
        "total": sum(row["rating"] for row in by_name[:3]),
        "same": sum(row["rating"] for row in by_name[:3]),
        "books": 3,
    }
    with pytest.raises(QueryError, match="doesn't select"):
        await sliced.aggregate(total=Sum("id"))


@pytest.mark.asyncio
async def test_aggregate_rejects_an_annotation_a_flat_values_list_does_not_select(library):
    """A flat values_list() keeps its one column - an annotation grouping it isn't a column of the
    rows aggregate() reads."""
    groups = _group_books(library["rows"], "author_id")
    per_author = Book.objects.all().values_list("author_id", flat=True).annotate(books=Count("id"))
    with pytest.raises(QueryError, match="doesn't select"):
        await per_author.aggregate(total=Sum("books"))
    assert await per_author.aggregate(authors=Count("author_id")) == {"authors": len(groups)}
    assert await Book.objects.all().values_list("author_id").annotate(books=Count("id")).aggregate(
        total=Sum("books")
    ) == {"total": len(library["rows"])}


@pytest.mark.asyncio
async def test_aggregate_over_rows_multiplied_by_to_many_relation(library):
    rows = library["rows"]
    per_book = Author.objects.all().values("name", "books__rating")
    assert await per_book.count() == len(rows) + 1  # cid has no book - one row with NULL
    assert await per_book.aggregate(total=Sum("books__rating")) == {"total": sum(row["rating"] for row in rows)}


# ---------------------------------------------------------------------------
# iterator() / stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("shape_name", SHAPES)
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 100])
async def test_iterator_pages_every_shape(library, shape_name, chunk_size):
    make_values, make_output = SHAPES[shape_name]
    rows = library["rows"]
    by_rating = sorted(rows, key=lambda row: (row["rating"], row["id"]))

    # A tying ordering gets the primary key as a tie-breaker.
    assert [row async for row in make_values(Book.objects.all()).order_by("rating").iterator(chunk_size)] == [
        make_output(row) for row in by_rating
    ]
    sliced = make_values(Book.objects.all()).order_by("rating")[1:6]
    assert [row async for row in sliced.iterator(chunk_size)] == [make_output(row) for row in by_rating[1:6]]
    after = make_values(Book.objects.all()).order_by("name").after_cursor("beta")
    assert [row async for row in after.iterator(chunk_size)] == [
        make_output(row) for row in sorted(rows, key=lambda row: row["name"]) if row["name"] > "beta"
    ]
    assert [row async for row in make_values(Book.objects.all()).none().order_by("name").iterator(chunk_size)] == []


@pytest.mark.asyncio
async def test_iterator_pages_grouped_and_distinct_rows(library):
    grouped = Book.objects.all().values("subject").annotate(books=Count("id")).exclude(subject=None).order_by("-books")
    assert [row async for row in grouped.iterator(chunk_size=1)] == sorted(
        await grouped, key=lambda row: (-row["books"], row["subject"])
    )
    ratings = Book.objects.all().values_list("rating", flat=True).distinct().order_by("-rating")
    assert [rating async for rating in ratings.iterator(chunk_size=2)] == sorted(
        {row["rating"] for row in library["rows"]}, reverse=True
    )


@pytest.mark.asyncio
async def test_iterator_by_keyset_is_immune_to_deleted_rows(library):
    seen = []
    async for row in Book.objects.all().values("id", "name").order_by("name").iterator(chunk_size=2):
        seen.append(row["name"])
        if len(seen) == 2:
            await Book.objects.filter(name="alpha").delete()
    assert seen == sorted(row["name"] for row in library["rows"])


@pytest.mark.asyncio
async def test_iterator_without_order_by_pages_by_primary_key(library):
    expected = [{"name": row["name"]} for row in sorted(library["rows"], key=lambda row: row["id"])]
    assert [row async for row in Book.objects.all().values("name").iterator(chunk_size=2)] == expected
    grouped = Book.objects.all().values("author_id").annotate(books=Count("id")).group_by("author_id")
    expected_groups = sorted(Counter(row["author_id"] for row in library["rows"]).items())
    assert [(row["author_id"], row["books"]) async for row in grouped.iterator(chunk_size=1)] == expected_groups


@pytest.mark.asyncio
async def test_iterator_rejects_invalid_arguments(library):
    with pytest.raises(QueryError):
        [row async for row in Book.objects.all().values("name").order_by("name").iterator(chunk_size=0)]
    with pytest.raises(QueryError, match="before_cursor"):
        [row async for row in Book.objects.all().values("name").order_by("name").before_cursor("delta").iterator()]


@pytest.mark.asyncio
@requires_features(dialect="sqlite")
async def test_stream_raises_unsupported_on_sqlite(library):
    with pytest.raises(UnSupportedError, match="stream"):
        [row async for row in Book.objects.all().values("name").stream()]


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
@pytest.mark.parametrize("shape_name", SHAPES)
async def test_stream_yields_every_shape(library, shape_name):
    make_values, _make_output = SHAPES[shape_name]
    query = make_values(Book.objects.all()).order_by("name")
    async with Transactions.atomic():
        streamed = [row async for row in query.stream(chunk_size=2)]
        streamed_union = [
            row
            async for row in query.union(make_values(Book.objects.filter(name="alpha")))
            .order_by(*_first_output_names(query))
            .stream()
        ]
    assert streamed == await query
    assert streamed_union == await query.union(make_values(Book.objects.filter(name="alpha"))).order_by(
        *_first_output_names(query)
    )


def _first_output_names(values_query):
    return values_query._get_compiler()._get_output_names_for_set_operation()[:1]


# ---------------------------------------------------------------------------
# union() / intersection() / difference()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_operations_of_values_lists(library):
    rows = library["rows"]
    ann_names = {row["name"] for row in rows if row["author_name"] == "ann"}
    rated_three = {row["name"] for row in rows if row["rating"] == 3}

    ann = Book.objects.filter(author__name="ann").values_list("name", flat=True)
    three = Book.objects.filter(rating=3).values_list("name", flat=True)
    assert await ann.union(three).order_by("name") == sorted(ann_names | rated_three)
    assert sorted(await ann.union(three, all=True)) == sorted([*ann_names, *rated_three])
    assert await ann.intersection(three).order_by("name") == sorted(ann_names & rated_three)
    assert await ann.difference(three).order_by("name") == sorted(ann_names - rated_three)
    assert await ann.union(three).order_by("-name") == sorted(ann_names | rated_three, reverse=True)

    combined = ann.union(three).order_by("name")
    expected = sorted(ann_names | rated_three)
    assert await combined.count() == len(expected)
    assert await combined[1:3] == expected[1:3]
    assert await combined[1:3].count() == 2
    assert await combined[1] == expected[1]
    with pytest.raises(IndexError):
        await combined[10]
    assert await combined.first() == expected[0]
    assert await combined.last() == expected[-1]
    assert await combined.exists() is True
    assert await ann.intersection(Book.objects.filter(name="eps").values_list("name", flat=True)).exists() is False
    assert await combined.aggregate(names=Count("name"), first_name=Min("name")) == {
        "names": len(expected),
        "first_name": expected[0],
    }
    assert [name async for name in combined.iterator(chunk_size=2)] == expected
    assert [name async for name in combined] == expected


@pytest.mark.asyncio
async def test_set_operation_rows_take_the_first_branch_shape(library):
    dict_first = (
        Book.objects.filter(name="alpha")
        .values("name", "rating")
        .union(Book.objects.filter(name="beta").values_list("name", "rating"))
    )
    assert await dict_first.order_by("name") == [{"name": "alpha", "rating": 5.0}, {"name": "beta", "rating": 3.0}]
    tuple_first = (
        Book.objects.filter(name="alpha")
        .values_list("name", "rating")
        .union(Book.objects.filter(name="beta").values("name", "rating"))
    )
    assert await tuple_first.order_by("-rating") == [("alpha", 5.0), ("beta", 3.0)]
    named = (
        Book.objects.filter(name="alpha")
        .values_list("name", "rating", named=True)
        .union(Book.objects.filter(name="beta").values_list("name", "rating"))
    )
    assert [row.name for row in await named.order_by("name")] == ["alpha", "beta"]
    cross_model = (
        Author.objects.all()
        .values_list("name", flat=True)
        .union(Book.objects.filter(author__name="ann").values_list("name", flat=True))
    )
    assert await cross_model.order_by("name") == sorted(["ann", "bob", "cid", "alpha", "beta", "gamma"])


@pytest.mark.asyncio
async def test_set_operation_as_subquery_filter_value(library):
    rows = library["rows"]
    top_or_low = (
        Book.objects.filter(rating__gte=5)
        .values_list("author_id", flat=True)
        .union(Book.objects.filter(rating__lte=1).values_list("author_id", flat=True))
    )
    expected_ids = {row["author_id"] for row in rows if row["rating"] >= 5 or row["rating"] <= 1}
    assert {author.id for author in await Author.objects.filter(id__in=top_or_low)} == expected_ids
    assert {author.id for author in await Author.objects.exclude(id__in=top_or_low)} == {
        author.id for author in library["authors"].values()
    } - expected_ids
    assert await Book.objects.filter(author__in=top_or_low).count() == len(
        [row for row in rows if row["author_id"] in expected_ids]
    )


@pytest.mark.asyncio
async def test_multi_column_filter_value_is_rejected(library):
    """Like Django, a subquery compared with one column must select one - not a raw database
    error at execution."""
    two_columns = Book.objects.filter(rating__gte=4).values_list("author_id", "name")
    multi_column_values = (
        two_columns,
        Book.objects.filter(rating__gte=4).values("author_id", "name"),
        Book.objects.all().values("author_id").annotate(books=Count("id")),
        two_columns.union(Book.objects.filter(rating__lte=1).values_list("author_id", "name")),
    )
    for value in multi_column_values:
        for filtering in (
            lambda: Author.objects.filter(id__in=value),
            lambda: Author.objects.exclude(id__in=value),
            lambda: Author.objects.filter(id__not_in=value),
            lambda: Book.objects.filter(author__in=value),
            lambda: Book.objects.filter(author__id__in=value),
            lambda: Author.objects.filter(id__in=Subquery(value)),
        ):
            with pytest.raises(QueryError, match="Cannot use multi-field values as a filter value"):
                await filtering()
    assert await Author.objects.filter(id__in=Book.objects.filter(rating__gte=5).values("author_id")).count() == 1


@pytest.mark.asyncio
async def test_set_operation_of_ordered_sliced_and_nested_branches(library):
    rows = library["rows"]
    best_two = Book.objects.all().order_by("-rating", "name").values_list("name", flat=True)[:2]
    worst = Book.objects.all().order_by("rating").values_list("name", flat=True)[:1]
    assert await best_two.union(worst).order_by("name") == sorted(["alpha", "delta", "eps"])

    ann = Book.objects.filter(author__name="ann").values_list("name", flat=True)
    three = Book.objects.filter(rating=3).values_list("name", flat=True)
    art = Book.objects.filter(subject="art").values_list("name", flat=True)
    ann_names = {row["name"] for row in rows if row["author_name"] == "ann"}
    three_names = {row["name"] for row in rows if row["rating"] == 3}
    art_names = {row["name"] for row in rows if row["subject"] == "art"}
    assert await ann.union(three.intersection(art)).order_by("name") == sorted(ann_names | (three_names & art_names))
    # INTERSECT applies to the union chained before it.
    assert await ann.union(art).intersection(three).order_by("name") == sorted((ann_names | art_names) & three_names)
    # Chaining onto a sliced set operation combines it as one branch.
    assert await ann.union(art).order_by("name")[:2].union(three).order_by("name") == sorted(
        set(sorted(ann_names | art_names)[:2]) | three_names
    )


@pytest.mark.asyncio
async def test_set_operation_errors(library):
    names = Book.objects.all().values_list("name", flat=True)
    with pytest.raises(QueryError, match="same number of columns"):
        await names.union(Book.objects.all().values_list("name", "rating"))
    with pytest.raises(QueryError, match="values"):
        names.union(Book.objects.all())
    with pytest.raises(QueryError, match="values"):
        Book.objects.all().union(names)
    with pytest.raises(QueryError, match="output names"):
        names.union(names).order_by("rating")
    with pytest.raises(QueryError):
        names.union(names).filter(name="alpha")
    with pytest.raises(QueryError):
        names.union(names).update(name="x")


@pytest.mark.asyncio
async def test_union_query_values_and_values_list(library):
    rows = library["rows"]
    union = Book.objects.filter(author__name="ann").union(Book.objects.filter(rating__lte=2)).order_by("name")
    expected = sorted(
        (row for row in rows if row["author_name"] == "ann" or row["rating"] <= 2), key=lambda row: row["name"]
    )
    assert union.values("name")._combination is not None
    assert await union.values("name", "rating") == [{"name": row["name"], "rating": row["rating"]} for row in expected]
    assert await union.values_list("name", flat=True) == [row["name"] for row in expected]
    assert await union.limit(2).values_list("name", flat=True) == [row["name"] for row in expected][:2]
    assert await union.values_list("name", flat=True).count() == len(expected)
    with pytest.raises(QueryError, match="doesn't select"):
        union.values("rating")


# ---------------------------------------------------------------------------
# update() and the methods values() rejects, like Django
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_after_values_updates_the_source_rows(library):
    updated = await Book.objects.filter(author__name="bob").values("name").update(subject="new")
    assert updated == len([row for row in library["rows"] if row["author_name"] == "bob"])
    assert await Book.objects.filter(subject="new").count() == updated
    assert await Book.objects.all().order_by("id").values_list("name", flat=True)[:2].update(subject="first") == 2
    assert await Book.objects.filter(subject="first").values_list("name", flat=True).order_by("id") == [
        "alpha",
        "beta",
    ]


@pytest.mark.asyncio
async def test_update_after_values_rejects_ambiguous_rows(library):
    with pytest.raises(QueryError, match="grouped"):
        Book.objects.all().values("author_id").annotate(books=Count("id")).update(subject="x")
    with pytest.raises(QueryError, match="grouped"):
        Book.objects.all().values("author_id").group_by("author_id").update(subject="x")
    with pytest.raises(QueryError, match="sliced"):
        Book.objects.all().values("subject").distinct().order_by("subject")[:1].update(subject="x")


@pytest.mark.asyncio
async def test_rejected_methods(library):
    values_query = Book.objects.all().values("name")
    for method_name, arguments in (
        ("delete", ()),
        ("contains", (Book(),)),
        ("only", ("name",)),
        ("defer", ("name",)),
        ("select_related", ("author",)),
    ):
        with pytest.raises(QueryError, match=method_name):
            getattr(values_query, method_name)(*arguments)
    with pytest.raises(QueryError, match="prefetch_related"):
        values_query.prefetch_related("author")


@pytest.mark.asyncio
async def test_last_latest_get_on_sliced_grouped_query_raise(library):
    grouped = Book.objects.all().values("author_id").annotate(books=Count("id")).order_by("author_id")[:1]
    for method in (lambda: grouped.last(), lambda: grouped.latest("author_id"), lambda: grouped.get(author_id=1)):
        with pytest.raises(QueryError, match="sliced"):
            method()
    # first() of a slice is its first row.
    assert await grouped.first() == (await grouped)[0]
    # A sliced query whose rows are the model rows narrows fine.
    by_name = sorted(library["rows"], key=lambda row: row["name"])
    assert await Book.objects.all().values_list("name", flat=True).order_by("name")[1:4].last() == by_name[3]["name"]


# ---------------------------------------------------------------------------
# values() again, expressions, annotations, CTEs, locking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_values_again_replaces_the_selection(library):
    assert await Book.objects.all().values("name").values_list("rating", flat=True).order_by("rating") == sorted(
        row["rating"] for row in library["rows"]
    )
    assert await Book.objects.filter(name="alpha").values_list("name").values("id", "name") == [
        {"id": library["rows"][0]["id"], "name": "alpha"}
    ]
    assert await Book.objects.filter(name="alpha").values(doubled=F("rating") * 2).values_list("doubled", "name") == [
        (10.0, "alpha")
    ]
    assert await Book.objects.all().values(doubled=F("rating") * 2).filter(doubled__gt=6).order_by(
        "-doubled"
    ).values_list("name", flat=True) == ["alpha", "delta"]


@pytest.mark.asyncio
async def test_values_again_keeps_the_implicit_grouping(library):
    """values(...).annotate(aggregate) groups by the selected fields - selecting other columns
    afterwards keeps that grouping, like Django."""
    groups = _group_books(library["rows"], "author_id")
    book_counts = sorted(len(group) for group in groups.values())
    grouped = Book.objects.all().values("author_id").annotate(books=Count("id"))

    assert sorted(await grouped.values_list("books", flat=True)) == book_counts
    assert await grouped.filter(books__gt=3).values_list("books", flat=True) == [4]
    assert await grouped.order_by("-books").values("books")[:1] == [{"books": 4}]
    assert await grouped.values_list("books", flat=True).count() == len(groups)
    assert await grouped.order_by("books").values_list("books", flat=True).first() == 3
    assert await grouped.order_by("author_id").values("books", "author_id") == [
        {"books": len(group), "author_id": author_id} for author_id, group in sorted(groups.items())
    ]
    renamed = Book.objects.all().values(writer="author__name").annotate(books=Count("id"))
    assert await renamed.order_by("books").values_list("books", flat=True) == book_counts
    by_expression = Book.objects.all().values(doubled=F("rating") * 2).annotate(books=Count("id"))
    assert sorted(await by_expression.values_list("books", flat=True)) == sorted(
        Counter(row["rating"] for row in library["rows"]).values()
    )


@pytest.mark.asyncio
async def test_renamed_field_is_readable_by_its_new_name(library):
    """values(title="name") can be filtered and ordered by "title", like an expression kwarg."""
    renamed = Book.objects.all().values(title="name")
    names = sorted(row["name"] for row in library["rows"])

    assert await renamed.filter(title="alpha") == [{"title": "alpha"}]
    assert await renamed.exclude(title__startswith="e").order_by("-title").values_list("title", flat=True) == sorted(
        (name for name in names if not name.startswith("e")), reverse=True
    )
    assert await renamed.order_by("title").first() == {"title": names[0]}
    assert await renamed.filter(title=F("name")).count() == len(names)
    assert await Book.objects.all().values(writer="author__name").filter(writer="bob").count() == 4
    # A new name that is a field of the model keeps meaning that field.
    assert await Book.objects.all().values(name="subject").filter(name="alpha") == [{"name": "sci"}]


@pytest.mark.asyncio
async def test_annotate_after_values_adds_columns(library):
    rows = await Book.objects.filter(name="alpha").values("name").annotate(half=F("rating") / 2)
    assert rows == [{"name": "alpha", "half": 2.5}]
    rows = await Book.objects.filter(name="alpha").values_list("name").annotate(half=F("rating") / 2)
    assert rows == [("alpha", 2.5)]
    assert await Book.objects.filter(name="alpha").values().annotate(half=F("rating") / 2).values_list(
        "half", flat=True
    ) == [2.5]
    with pytest.raises(FieldError):
        Book.objects.all().values("name").annotate(rating=F("rating"))


@pytest.mark.asyncio
async def test_with_cte_chained_after_values(library):
    top = Book.objects.filter(rating__gte=4).values("id")
    names = (
        Book.objects.all()
        .values_list("name", flat=True)
        .with_cte("top_books", top)
        .filter(id__in=RawSQL('SELECT id FROM "top_books"'))
    )
    assert sorted(await names) == sorted(row["name"] for row in library["rows"] if row["rating"] >= 4)
    assert await names.count() == 2


@pytest.mark.asyncio
@requires_features(supports_select_for_update=True)
async def test_select_for_update_chained_after_values(library):
    async with Transactions.atomic() as connection:
        rows = await Book.objects.filter(name="alpha").values("name").select_for_update().using(connection)
        assert rows == [{"name": "alpha"}]
        assert await Book.objects.filter(rating=3).values("name").select_for_update().using(connection).count() == 3


# ---------------------------------------------------------------------------
# QuerySet integer index, and query shape cache keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queryset_integer_index(library):
    by_name = sorted(library["rows"], key=lambda row: row["name"])
    book = await Book.objects.all().order_by("name")[2]
    assert book.name == by_name[2]["name"]
    assert (await Book.objects.all().order_by("name")[1:][1]).name == by_name[2]["name"]
    with pytest.raises(IndexError):
        await Book.objects.all().order_by("name")[20]
    with pytest.raises(QueryError):
        Book.objects.all()[-1]
    with pytest.raises(QueryError):
        Book.objects.all()["name"]


@pytest.mark.asyncio
async def test_set_operation_branch_shape_never_reused_for_plain_query(db):
    # Team has Meta.ordering = ["id"]; a set operation branch is built without it.
    for team_id in (3, 1, 2):
        await Team.objects.create(id=team_id, name=f"team {team_id}")
    ids = Team.objects.all().values_list("id", flat=True)
    for _ in range(2):
        assert sorted(await ids.union(ids)) == [1, 2, 3]
        assert await ids == [1, 2, 3]


@pytest.mark.asyncio
async def test_chained_filters_reuse_query_shape_cache_without_leaking_values(library):
    rows = library["rows"]
    for threshold in (1, 4, 2, 5, 1):
        expected = sorted(row["name"] for row in rows if row["rating"] >= threshold)
        names = Book.objects.all().values_list("name", flat=True).filter(rating__gte=threshold).order_by("name")
        assert await names == expected
        assert await names.count() == len(expected)
        assert await names.exists() is bool(expected)
        assert await names.first() == expected[0]
        grouped = Book.objects.all().values("author_id").annotate(books=Count("id")).filter(rating__gte=threshold)
        assert sorted(row["books"] for row in await grouped) == sorted(
            Counter(row["author_id"] for row in rows if row["rating"] >= threshold).values()
        )


@pytest.mark.asyncio
async def test_count_and_exists_then_set_operation_of_the_same_shape(library):
    # count()/exists() select from the built query as a derived table - selecting from it must
    # never alias the query the query shape cache keeps, which a set operation branch reuses.
    names = Book.objects.filter(name="alpha").values_list("name", flat=True)
    for _ in range(2):
        assert await names.count() == 1
        assert await names.exists() is True
        assert await names.aggregate(longest=Max("name")) == {"longest": "alpha"}
        other = Book.objects.filter(name="beta").values_list("name", flat=True)
        assert await names.union(other).order_by("name") == ["alpha", "beta"]
        assert await Book.objects.filter(name="alpha").values("name").count() == 1
        assert await Book.objects.filter(name="alpha").values("name").union(
            Book.objects.filter(name="beta").values("name")
        ).order_by("name") == [{"name": "alpha"}, {"name": "beta"}]


# ---------------------------------------------------------------------------
# Self-consistency across row-set shapes: every terminal method agrees with the awaited rows.
# ---------------------------------------------------------------------------


def _best_book_per_author():
    return Window(WindowRowNumber(), partition_by=["author_id"], order_by=["-rating", "id"])


ROW_SET_VARIANTS = {
    "plain": lambda: Book.objects.all().values("name", "rating").order_by("name"),
    "flat_filtered": lambda: Book.objects.filter(rating__gte=2).values_list("name", flat=True).order_by("-name"),
    "named": lambda: Book.objects.all().values_list("name", "subject", named=True).order_by("-rating", "id"),
    "expression_ordering": lambda: (
        Book.objects.all().values("name", doubled=F("rating") * 2).order_by("-doubled", "name")
    ),
    "grouped": lambda: (
        Book.objects.all().values("author_id").annotate(books=Count("id"), best=Max("rating")).order_by("author_id")
    ),
    "grouped_having": lambda: (
        Book.objects.all()
        .values_list("subject")
        .annotate(books=Count("id"))
        .filter(books__gte=2)
        .order_by("-books", "subject")
    ),
    "group_by": lambda: (
        Book.objects.all().values("author_id").group_by("author_id").annotate(total=Sum("rating")).order_by("-total")
    ),
    "distinct_selected": lambda: Book.objects.all().values_list("rating", flat=True).distinct().order_by("-rating"),
    "distinct_first_occurrence": lambda: Book.objects.all().values("subject").distinct().order_by("-rating", "id"),
    "to_many_rows": lambda: Author.objects.all().values("name", "books__name").order_by("name", "books__name"),
    "sliced": lambda: Book.objects.all().values_list("name", "rating").order_by("name")[1:6],
    "sliced_distinct_first_occurrence": lambda: (
        Book.objects.all().values_list("subject", flat=True).distinct().order_by("rating", "id")[1:]
    ),
    "after_cursor": lambda: Book.objects.all().values("name").order_by("name").after_cursor("beta"),
    "window_filter": lambda: (
        Book.objects.all()
        .annotate(position=_best_book_per_author())
        .filter(position=1)
        .values("name", "author_id")
        .order_by("name")
    ),
    "window_filter_distinct": lambda: (
        Book.objects.all()
        .annotate(position=_best_book_per_author())
        .filter(position__lte=2)
        .values_list("subject", flat=True)
        .distinct()
        .order_by("-rating", "id")
    ),
    "union": lambda: (
        Book.objects.filter(author__name="ann")
        .values_list("name", "rating")
        .union(Book.objects.filter(rating__lte=2).values_list("name", "rating"))
        .order_by("name")
    ),
    "union_sliced": lambda: (
        Book.objects.all()
        .values_list("name", flat=True)
        .union(Author.objects.all().values_list("name", flat=True))
        .order_by("-name")[2:7]
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant_name", ROW_SET_VARIANTS)
async def test_terminal_methods_agree_with_awaited_rows(library, variant_name):
    make_query = ROW_SET_VARIANTS[variant_name]
    rows = await make_query()
    assert rows, "every variant returns rows"

    assert await make_query().count() == len(rows)
    assert await make_query().exists() is True
    assert await make_query().first() == rows[0]
    assert await make_query()[0] == rows[0]
    assert await make_query()[len(rows) - 1] == rows[-1]
    with pytest.raises(IndexError):
        await make_query()[len(rows)]
    assert await make_query()[1:3] == rows[1:3]
    assert await make_query()[1:3].count() == len(rows[1:3])
    assert await make_query()[len(rows) :].exists() is False
    assert await make_query()[len(rows) :].count() == 0
    assert await make_query()[1:][:2] == rows[1:3]
    assert [row async for row in make_query()] == rows
    for chunk_size in (1, 2, 50):
        assert [row async for row in make_query().iterator(chunk_size)] == rows
    assert await make_query().none() == []
    assert await make_query().none().count() == 0
    assert await make_query().none().exists() is False
    query = make_query()
    if not (query._limit is not None or query._offset or query._cursor_values):
        assert await make_query().last() == rows[-1]


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
@pytest.mark.parametrize("variant_name", ROW_SET_VARIANTS)
async def test_stream_agrees_with_awaited_rows(library, variant_name):
    make_query = ROW_SET_VARIANTS[variant_name]
    rows = await make_query()
    async with Transactions.atomic():
        assert [row async for row in make_query().stream(chunk_size=2)] == rows


@pytest.mark.asyncio
async def test_first_and_last_of_distinct_rows_are_the_ends_of_the_first_occurrence_rows(library):
    rows = library["rows"]
    by_rating = _first_occurrences(
        [row["subject"] for row in sorted(rows, key=lambda row: (-row["rating"], row["id"]))]
    )
    subjects = Book.objects.all().values_list("subject", flat=True).distinct().order_by("-rating", "id")
    assert await subjects.first() == by_rating[0]
    assert await subjects.last() == by_rating[-1]
    # Unordered: the primary key orders them.
    by_id = _first_occurrences([row["subject"] for row in sorted(rows, key=lambda row: row["id"])])
    unordered = Book.objects.all().values("subject").distinct()
    assert await unordered.first() == {"subject": by_id[0]}
    assert await unordered.last() == {"subject": by_id[-1]}
    # Ordered by the selected field: a plain reversal.
    names = Book.objects.all().values_list("author__name", flat=True).distinct().order_by("author__name")
    assert await names.first() == "ann"
    assert await names.last() == "bob"


@pytest.mark.asyncio
async def test_empty_set_operation(library):
    names = Book.objects.all().values_list("name", flat=True)
    empty = names.union(names).none()
    assert await empty == []
    assert await empty.count() == 0
    assert await empty.exists() is False
    assert await empty.first() is None
    assert await empty.aggregate(names=Count("name")) == {"names": 0}
    assert [name async for name in empty.order_by("name").iterator()] == []
    assert await empty.union(Book.objects.filter(name="alpha").values_list("name", flat=True)) == ["alpha"]
    assert await Book.objects.filter(name__in=empty).count() == 0


FAN_OUT_SHAPES = {
    # Grouped by a to-many field while aggregating over the same relation.
    "group_by_to_many_field": (("books__subject",), {"book_count": Count("books")}),
    "group_by_to_many_field_distinct_count": (("books__subject",), {"book_count": Count("books", distinct=True)}),
    # Grouped by a forward field while aggregating over a to-many relation.
    "group_by_own_field": (("name",), {"total": Sum("books__rating")}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("fan_out_shape", FAN_OUT_SHAPES)
async def test_annotate_before_values_matches_grouping_per_model_row_under_the_fan_out_guard(library, fan_out_shape):
    """Like Django, an aggregate annotated before values() is computed per model row - the same
    rows as grouping by the primary key and the selected fields, under the fan-out guard too."""
    selected_names, annotations = FAN_OUT_SHAPES[fan_out_shape]

    async def get_outcome(make_query, dropped_names=()):
        try:
            rows = await make_query()
        except ConfigurationError as error:
            return ("error", type(error))
        rows = [{key: value for key, value in row.items() if key not in dropped_names} for row in rows]
        return ("rows", sorted(rows, key=repr))

    per_model_row = await get_outcome(
        lambda: Author.objects.all().values("id", *selected_names).annotate(**annotations), dropped_names=("id",)
    )
    annotated_first = await get_outcome(
        lambda: Author.objects.all().annotate(**annotations).values(*selected_names, *annotations)
    )
    assert per_model_row == annotated_first


@pytest.mark.asyncio
async def test_values_renaming_an_annotation_keeps_it_through_chained_methods(library):
    """values(key="annotation") lost the column (or failed) once filter()/exclude()/order_by()/
    distinct()/first()/get() was chained - the key stayed an unselected alias of the annotation."""
    ann, bob, cid = (library["authors"][name].id for name in ("ann", "bob", "cid"))
    counted = Author.objects.all().annotate(n=Count("books")).values("id", c="n")

    assert await counted.filter(id=ann) == [{"id": ann, "c": 3}]
    assert await counted.filter(c=4) == [{"id": bob, "c": 4}]
    assert await counted.order_by("-c")[:1] == [{"id": bob, "c": 4}]
    assert await counted.order_by("id").first() == {"id": ann, "c": 3}
    assert await counted.get(id=cid) == {"id": cid, "c": 0}
    assert await counted.exclude(id__in=[ann, bob]) == [{"id": cid, "c": 0}]
    assert await counted.distinct().order_by("id")[2:] == [{"id": cid, "c": 0}]
    shifted = Book.objects.all().annotate(x=F("rating") + 1).values("name", y="x")
    assert await shifted.filter(name="alpha") == [{"name": "alpha", "y": 6.0}]
    assert await shifted.order_by("-y", "name")[:1] == [{"name": "alpha", "y": 6.0}]


@pytest.mark.asyncio
async def test_window_over_a_grouped_values_query_is_not_a_group_key(library):
    """A window function selected by a grouped values query landed in the GROUP BY once the query
    was re-selected - rejected by the database."""
    ann, bob = (library["authors"][name].id for name in ("ann", "bob"))
    ranked = Book.objects.all().values("author_id").annotate(n=Count("id"), r=Window(WindowRank(), order_by=["-n"]))

    assert sorted(await ranked.values_list("r", flat=True)) == [1, 2]
    assert await ranked.order_by("author_id").values_list("author_id", "r") == [(ann, 2), (bob, 1)]
    assert await ranked.filter(r=1).values_list("author_id", flat=True) == [bob]
    assert [
        row async for row in ranked.order_by("author_id").values_list("author_id", "r").iterator(chunk_size=1)
    ] == [(ann, 2), (bob, 1)]
    running = (
        Book.objects.all()
        .values("author_id")
        .annotate(s=Sum("rating"), c=Window(WindowSum("s"), order_by=["author_id"]))
    )
    assert await running.order_by("author_id").values_list("author_id", "c") == [(ann, 11.0), (bob, 21.0)]


@pytest.mark.asyncio
async def test_window_filter_survives_reselecting_and_aggregating(library):
    """A filter on a window function of a values query failed once the query was re-selected or
    aggregated - the check meant for model querysets ran on it."""
    numbered = (
        Book.objects.all().values("id", position=Window(WindowRowNumber(), order_by=["id"])).filter(position__gte=6)
    )
    last_ids = sorted(row["id"] for row in library["rows"])[5:]

    assert sorted(await numbered.values_list("id", flat=True)) == last_ids
    assert await numbered.aggregate(books=Count("id")) == {"books": 2}


@pytest.mark.asyncio
async def test_aggregate_annotated_before_values_is_computed_per_model_row(library):
    """Like Django, an aggregate annotated before values()/values_list() - or passed to them - is
    computed per model row; one annotated after them groups the rows by the selected fields."""
    assert sorted(await Author.objects.all().annotate(n=Count("books")).values_list("n", flat=True)) == [0, 3, 4]
    assert await Author.objects.all().annotate(n=Count("books")).values("n").count() == 3
    assert sorted(await Author.objects.all().values_list(n=Count("books"), flat=True)) == [0, 3, 4]
    assert sorted(
        await Author.objects.all().alias(n=Count("books")).filter(n__gte=3).values_list("name", flat=True)
    ) == [
        "ann",
        "bob",
    ]
    assert sorted(
        await Book.objects.all()
        .values("subject")
        .alias(n=Count("id"))
        .filter(n__gte=3)
        .values_list("subject", flat=True)
    ) == ["art", "sci"]
    assert sorted(
        await Book.objects.all().values("subject").annotate(n=Count("id")).values_list("subject", "n"), key=repr
    ) == [
        ("art", 3),
        ("sci", 3),
        (None, 1),
    ]


@pytest.mark.asyncio
async def test_row_picked_from_a_slice_cannot_be_filtered_or_reordered(library):
    """last()/latest()/earliest()/get()/get_or_none() of a slice returned a queryset that could be
    filtered and reordered, unlike first()/[i] of a slice."""
    sliced = Book.objects.all().order_by("id")[:3]
    for single_row in (sliced.last(), sliced.latest("rating"), sliced.earliest("rating"), sliced.get_or_none()):
        with pytest.raises(QueryError):
            single_row.filter(rating__gt=1)
        with pytest.raises(QueryError):
            single_row.order_by("name")
    with pytest.raises(QueryError):
        Book.objects.all().order_by("id")[2:3].get().filter(rating=1)
    with pytest.raises(QueryError):
        Book.objects.all().order_by("id").values_list("name", flat=True)[:3].last().order_by("-id")
    assert (await sliced.last()).name == "gamma"
    assert (await Book.objects.all().order_by("id").last()).name == "eta"
    assert (await Book.objects.all().order_by("id").last().filter(rating__gt=3)).name == "delta"
