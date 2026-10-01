"""count()/exists()/first()/get()/update()/delete()/iterator() agree with the rows awaiting the
same queryset returns."""

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import QueryError
from hare.query.expressions import Case, Exists, F, OuterRef, Subquery, Value, When
from hare.query.functions import Count, Lower, Max, Sum
from tests.testmodels import (
    Author,
    Book,
    CompositePkThing,
    IntFields,
    SoftDeleteParent,
    SoftDeleteStandalone,
    UniqueName,
)


async def create_authors_with_books() -> tuple[Author, Author]:
    """Two authors: the first with three books (ratings 1-3), the second with two (ratings 4-5)."""
    first_author = await Author.objects.create(name="first")
    second_author = await Author.objects.create(name="second")
    for rating in (1.0, 2.0, 3.0):
        await Book.objects.create(name=f"book {rating}", author=first_author, rating=rating)
    for rating in (4.0, 5.0):
        await Book.objects.create(name=f"book {rating}", author=second_author, rating=rating)
    return first_author, second_author


@pytest.mark.asyncio
async def test_sliced_distinct_update_touches_the_rows_the_slice_returns(db):
    await create_authors_with_books()
    queryset = Book.objects.filter(author__books__rating__gt=0).distinct().order_by("id")
    shown_ids = [book.id for book in await queryset[1:3]]

    updated_count = await queryset[1:3].update(subject="picked")

    assert updated_count == 2
    assert await Book.objects.filter(subject="picked").order_by("id").values_list("id", flat=True) == shown_ids


@pytest.mark.asyncio
async def test_sliced_distinct_delete_removes_the_rows_the_slice_returns(db):
    await create_authors_with_books()
    queryset = Book.objects.filter(author__books__rating__gt=0).distinct().order_by("-rating")
    shown_ids = [book.id for book in await queryset[0:2]]

    deleted_count = await queryset[0:2].delete()

    assert deleted_count == 2
    remaining_ids = set(await Book.objects.all().values_list("id", flat=True))
    assert not remaining_ids & set(shown_ids)
    assert len(remaining_ids) == 3


@pytest.mark.asyncio
async def test_sliced_update_and_delete_ordered_by_aggregate_annotation(db):
    first_author, second_author = await create_authors_with_books()
    queryset = Author.objects.annotate(book_count=Count("books")).order_by("-book_count")
    assert [author.id for author in await queryset[0:1]] == [first_author.id]

    assert await queryset[0:1].update(name="most books") == 1
    assert (await Author.objects.get(id=first_author.id)).name == "most books"
    assert (await Author.objects.get(id=second_author.id)).name == "second"

    assert await queryset[1:2].delete() == 1
    assert await Author.objects.all().values_list("id", flat=True) == [first_author.id]


@pytest.mark.asyncio
async def test_single_row_methods_on_sliced_distinct_queryset(db):
    await create_authors_with_books()
    queryset = Book.objects.all().distinct().order_by("-rating")
    top_two = await queryset[0:2]
    assert [book.rating for book in top_two] == [5.0, 4.0]

    assert (await queryset[0:2].last()).rating == 4.0
    assert (await queryset[0:2].earliest("rating")).rating == 4.0
    assert (await queryset[0:2].latest("rating")).rating == 5.0
    assert (await queryset[0:1].get()).rating == 5.0
    with pytest.raises(QueryError, match="Cannot filter a query once a slice has been taken"):
        queryset[0:2].get(rating=1.0)


@pytest.mark.asyncio
async def test_sliced_distinct_values_subquery_ordered_by_unselected_field(db):
    await create_authors_with_books()
    subquery = Book.objects.all().distinct().order_by("-rating")[0:2].values_list("id", flat=True)
    expected_ids = await subquery

    assert sorted(await Book.objects.filter(id__in=subquery).values_list("id", flat=True)) == sorted(expected_ids)
    assert await Book.objects.filter(id__in=subquery).count() == 2


@pytest.mark.asyncio
async def test_count_and_exists_match_rows_of_to_many_ordering(db):
    await create_authors_with_books()
    queryset = Author.objects.all().order_by("-books__rating")
    assert len(await queryset) == 5

    assert await queryset.count() == 5
    assert await queryset[1:3].count() == len(await queryset[1:3]) == 2
    assert await queryset[2:].count() == len(await queryset[2:]) == 3
    assert await queryset[2:].exists() is True
    assert await queryset[5:].exists() is False

    distinct_queryset = Author.objects.all().order_by("books__rating").distinct()
    assert await distinct_queryset.count() == len(await distinct_queryset) == 5


@pytest.mark.asyncio
async def test_count_of_distinct_on_is_not_served_from_plain_distinct_cache(db):
    await create_authors_with_books()
    assert await Book.objects.all().distinct().count() == 5
    if Book._meta.db.dialect.name == "postgresql":
        assert await Book.objects.all().order_by("author_id").distinct("author_id").count() == 2


@pytest.mark.asyncio
async def test_exists_annotation_of_none_queryset_is_not_served_from_cache(db):
    author = await Author.objects.create(name="solo")
    await Book.objects.create(name="only", author=author, rating=1.0)
    book_filter = Book.objects.filter(name="only")

    assert await Author.objects.annotate(has_book=Exists(book_filter.none())).values_list("has_book", flat=True) == [
        False
    ]
    assert await Author.objects.annotate(has_book=Exists(book_filter)).values_list("has_book", flat=True) == [True]


@pytest.mark.asyncio
async def test_union_offset_without_limit(db):
    await create_authors_with_books()
    low = Book.objects.filter(rating__lte=3.0)
    high = Book.objects.filter(rating__gte=2.0)
    union = low.union(high).order_by("rating")

    assert [book.rating for book in await union.offset(3)] == [4.0, 5.0]
    assert await union.offset(3).count() == 2


@pytest.mark.asyncio
async def test_first_without_ordering_takes_the_lowest_primary_key(db):
    for primary_key in (3, 1, 2):
        await IntFields.objects.create(id=primary_key, intnum=primary_key)
    await IntFields.objects.filter(id=1).update(intnum=10)

    assert "ORDER BY" in IntFields.objects.first().sql()
    assert (await IntFields.objects.first()).id == 1
    assert (await IntFields.objects.all().last()).id == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4])
async def test_iterator_does_not_drop_rows_tying_on_the_ordering(db, chunk_size):
    for primary_key in range(1, 11):
        await IntFields.objects.create(
            id=primary_key, intnum=(primary_key + 1) // 2, intnum_null=None if primary_key % 3 else 5
        )
    for ordering in ("intnum", "-intnum", "intnum_null", "-intnum_null"):
        queryset = IntFields.objects.all().order_by(ordering)
        iterated_ids = [row.id async for row in queryset.iterator(chunk_size=chunk_size)]
        assert sorted(iterated_ids) == list(range(1, 11)), ordering


@pytest.mark.asyncio
async def test_iterator_pages_when_only_or_defer_leaves_the_ordering_field_unloaded(db):
    for primary_key in range(1, 8):
        await IntFields.objects.create(id=primary_key, intnum=primary_key // 2)

    only_rows = [row async for row in IntFields.objects.all().only("id").order_by("intnum").iterator(chunk_size=2)]
    deferred_rows = [
        row async for row in IntFields.objects.all().defer("intnum").order_by("intnum").iterator(chunk_size=3)
    ]

    for rows in (only_rows, deferred_rows):
        assert sorted(row.id for row in rows) == list(range(1, 8))
        for row in rows:
            with pytest.raises(AttributeError):
                _ = row.intnum
            assert not any(name.startswith("hare_iterator_cursor_") for name in vars(row))


@pytest.mark.asyncio
async def test_iterator_keeps_the_caller_cursor_and_offset_on_the_first_page(db):
    for primary_key in range(1, 9):
        await IntFields.objects.create(id=primary_key, intnum=(primary_key + 1) // 2)
    queryset = IntFields.objects.all().order_by("intnum").after_cursor(1)

    assert sorted([row.id async for row in queryset.iterator(chunk_size=2)]) == list(range(3, 9))


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_create_upsert_with_repeated_conflict_key_keeps_the_last_object(db):
    await UniqueName.objects.create(id=1, name="existing", optional="old")

    await UniqueName.objects.bulk_create(
        [
            UniqueName(id=10, name="new", optional="first"),
            UniqueName(id=11, name="existing", optional="first"),
            UniqueName(id=12, name="new", optional="second"),
            UniqueName(id=13, name="existing", optional="second"),
        ],
        update_fields=["optional"],
        on_conflict=["name"],
    )

    assert await UniqueName.objects.all().order_by("name").values_list("name", "optional") == [
        ("existing", "second"),
        ("new", "second"),
    ]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_bulk_create_upsert_returning_with_repeated_conflict_key(db):
    objects = [
        UniqueName(name="repeated", optional="first"),
        UniqueName(name="single", optional="only"),
        UniqueName(name="repeated", optional="second"),
    ]

    await UniqueName.objects.bulk_create(objects, update_fields=["optional"], on_conflict=["name"], returning=True)

    stored = {row.name: row for row in await UniqueName.objects.all()}
    assert stored["repeated"].optional == "second"
    assert [obj.pk for obj in objects] == [stored["repeated"].pk, stored["single"].pk, stored["repeated"].pk]


@pytest.mark.asyncio
async def test_values_list_named_rows(db):
    author = await Author.objects.create(name="named")

    rows = await Author.objects.filter(id=author.id).values_list("id", "name", named=True)

    assert rows[0].id == author.id
    assert rows[0].name == "named"
    assert rows == [(author.id, "named")]


@pytest.mark.asyncio
async def test_values_and_values_list_reject_invalid_arguments(db):
    with pytest.raises(QueryError):
        Author.objects.all().values_list("id", flat=True, named=True)
    with pytest.raises(QueryError):
        Author.objects.all().values_list("id", extra=5)
    with pytest.raises(QueryError):
        Author.objects.all().values("id", extra=5)


@pytest.mark.asyncio
async def test_sliced_update_and_delete_with_composite_primary_key(db):
    for revision in range(4):
        await CompositePkThing.objects.create(thing_id=1, revision=revision, name="original")
    queryset = CompositePkThing.objects.all().order_by("-revision")

    assert await queryset[1:3].update(name="updated") == 2
    assert await CompositePkThing.objects.filter(name="updated").order_by("revision").values_list(
        "revision", flat=True
    ) == [
        1,
        2,
    ]
    assert await queryset[0:1].delete() == 1
    assert await CompositePkThing.objects.all().order_by("revision").values_list("revision", flat=True) == [0, 1, 2]


@pytest.mark.asyncio
async def test_update_filtered_on_base_table_aggregate_touches_only_matching_rows(db):
    await create_authors_with_books()
    queryset = Book.objects.annotate(max_rating=Max("rating")).filter(max_rating__gt=4)
    assert [book.rating for book in await queryset] == [5.0]

    assert await queryset.update(subject="top") == 1
    assert await Book.objects.filter(subject="top").values_list("rating", flat=True) == [5.0]

    assert await Book.objects.annotate(book_count=Count("id")).filter(book_count__gte=2).update(subject="never") == 0
    assert await Book.objects.all().alias(max_rating=Max("rating")).filter(max_rating__lt=2).update(subject="low") == 1
    assert await Book.objects.filter(subject="low").values_list("rating", flat=True) == [1.0]
    assert await Book.objects.annotate(book_count=Count("id")).exclude(book_count=1).update(subject="never") == 0
    assert await Book.objects.filter(subject="never").count() == 0


@pytest.mark.asyncio
async def test_delete_filtered_on_base_table_aggregate_removes_only_matching_rows(db):
    await create_authors_with_books()
    queryset = Book.objects.annotate(total_rating=Sum("rating")).filter(total_rating__gte=4)
    matching_ids = {book.id for book in await queryset}

    assert await queryset.delete() == 2
    remaining_ids = set(await Book.objects.all().values_list("id", flat=True))
    assert len(remaining_ids) == 3
    assert not remaining_ids & matching_ids


@pytest.mark.asyncio
async def test_soft_delete_filtered_on_base_table_aggregate(db):
    for name in ("a", "bb", "ccc"):
        await SoftDeleteStandalone.objects.create(name=name)
    await SoftDeleteParent.objects.create(name="kept")
    await SoftDeleteParent.objects.create(name="gone")

    queryset = SoftDeleteStandalone.objects.annotate(max_id=Max("id")).filter(max_id__gt=0, name="bb")
    assert await queryset.delete() == 1
    assert sorted(await SoftDeleteStandalone.objects.all().values_list("name", flat=True)) == ["a", "ccc"]

    assert await SoftDeleteParent.objects.annotate(row_count=Count("id")).filter(row_count__gte=2).delete() == 0
    assert await SoftDeleteParent.objects.annotate(max_id=Max("id")).filter(max_id__gt=0, name="gone").delete() == 1
    assert await SoftDeleteParent.objects.all().values_list("name", flat=True) == ["kept"]


@pytest.mark.asyncio
async def test_bulk_update_filtered_on_aggregate_writes_only_matching_rows(db):
    first_author, second_author = await create_authors_with_books()
    books = await Book.objects.all().order_by("id")
    for book in books:
        book.subject = "bulk"
    await Book.objects.annotate(max_rating=Max("rating")).filter(max_rating__gt=4).bulk_update(books, ["subject"])
    assert await Book.objects.filter(subject="bulk").values_list("rating", flat=True) == [5.0]

    authors = [first_author, second_author]
    for author in authors:
        author.name = "renamed"
    await Author.objects.annotate(book_count=Count("books")).filter(book_count__gte=3).bulk_update(authors, ["name"])
    assert await Author.objects.filter(name="renamed").values_list("id", flat=True) == [first_author.id]


@pytest.mark.asyncio
async def test_count_of_distinct_queryset_counts_rows_an_annotation_over_to_many_keeps_apart(db):
    first_author, second_author = await create_authors_with_books()
    await Author.objects.create(name="no books")
    queryset = Author.objects.annotate(book_name=F("books__name")).distinct()

    rows = await queryset
    assert len(rows) == 6
    assert await queryset.count() == 6
    assert await queryset.order_by("id")[4:].count() == 2
    assert await queryset.order_by("id").offset(5).exists() is True
    assert await queryset.order_by("id").offset(6).exists() is False
    assert await Author.objects.filter(books__rating__gt=0).distinct().count() == 2

    sliced_ids = [author.id for author in await queryset.order_by("id", "book_name")[2:4]]
    assert await queryset.order_by("id", "book_name")[2:4].update(name="renamed") == len(set(sliced_ids))
    assert set(await Author.objects.filter(name="renamed").values_list("id", flat=True)) == set(sliced_ids)
    assert {first_author.id, second_author.id} >= set(sliced_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 100])
async def test_iterator_keeps_rows_repeating_a_primary_key(db, chunk_size):
    await create_authors_with_books()
    await Author.objects.create(name="no books")
    querysets = (
        Author.objects.annotate(book_name=F("books__name")).distinct().order_by("id"),
        Author.objects.annotate(book_name=Lower("books__name")).order_by("id"),
        Author.objects.annotate(next_rating=F("books__rating") + 1).order_by("id"),
        Author.objects.annotate(is_high=Case(When(books__rating__gt=2, then=Value(1)), default=Value(0))).order_by(
            "id"
        ),
        Author.objects.filter(books__rating__gt=1).order_by("id"),
        Author.objects.filter(books__rating__gt=1).distinct().order_by("id"),
    )
    for queryset in querysets:
        expected_rows = sorted((author.id, getattr(author, "book_name", None)) for author in await queryset)
        iterated_rows = sorted(
            [
                (author.id, getattr(author, "book_name", None))
                async for author in queryset.iterator(chunk_size=chunk_size)
            ]
        )
        assert iterated_rows == expected_rows


@pytest.mark.asyncio
async def test_in_filter_accepts_queryset_with_only_defer_select_related_prefetch(db):
    first_author, second_author = await create_authors_with_books()
    expected_ids = sorted(await Book.objects.filter(author=first_author).values_list("id", flat=True))
    author_querysets = (
        Author.objects.filter(name="first").only("id", "name"),
        Author.objects.filter(name="first").defer("name"),
        Author.objects.filter(name="first").prefetch_related("books"),
    )
    for author_queryset in author_querysets:
        assert (
            sorted(await Book.objects.filter(author__in=author_queryset).values_list("id", flat=True)) == expected_ids
        )
    book_querysets = (
        Book.objects.filter(author=first_author).select_related("author"),
        Book.objects.filter(author=first_author).only("id"),
    )
    for book_queryset in book_querysets:
        assert sorted(await Book.objects.filter(pk__in=book_queryset).values_list("id", flat=True)) == expected_ids


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_distinct_on_last_cursor_get_and_iterator_stay_within_its_rows(db):
    first_author, second_author = await create_authors_with_books()
    queryset = Book.objects.all().distinct("author_id").order_by("author_id", "-rating")
    rows = [(book.author_id, book.rating) for book in await queryset]
    assert rows == [(first_author.id, 3.0), (second_author.id, 5.0)]

    assert (await queryset.last()).rating == 5.0
    assert (await queryset.order_by("-author_id", "-rating").last()).rating == 3.0
    assert (await queryset.latest("author_id")).rating == 5.0
    # The condition applies before DISTINCT ON picks a row per author, as filter() does.
    assert (await queryset.get_or_none(rating=1.0)).rating == 1.0
    assert (await queryset.get(author_id=first_author.id)).rating == 3.0

    after_first = [(book.author_id, book.rating) for book in await queryset.after_cursor(first_author.id, 3.0)]
    assert after_first == [(second_author.id, 5.0)]
    before_second = [(book.author_id, book.rating) for book in await queryset.before_cursor(second_author.id, 5.0)]
    assert before_second == [(first_author.id, 3.0)]
    for chunk_size in (1, 2, 5):
        assert [(book.author_id, book.rating) async for book in queryset.iterator(chunk_size=chunk_size)] == rows


@pytest.mark.asyncio
async def test_distinct_over_a_parametrized_to_many_expression_counts_and_slices(db):
    """A plain .distinct() ordered by an expression with bind parameters selects it under an alias
    and orders by that alias - rendered twice, Postgres saw two different expressions."""
    first_author, second_author = await create_authors_with_books()
    bucket = Case(When(books__rating__gt=2, then=Value(1)), default=Value(0))
    bucketed = Author.objects.annotate(bucket=bucket).distinct()
    shifted = Author.objects.annotate(shifted=F("books__rating") + 1).distinct()

    assert await bucketed.count() == 3
    assert await shifted.count() == 5
    assert await shifted.offset(4).exists() is True
    assert await shifted.offset(5).exists() is False
    assert (await bucketed.order_by("id", "bucket").last()).id == second_author.id
    assert await bucketed.order_by("id")[:2].update(name="picked") == 1
    assert await Author.objects.filter(name="picked").values_list("id", flat=True) == [first_author.id]


@pytest.mark.asyncio
async def test_outer_ref_through_a_to_many_relation_repeats_rows(db):
    """OuterRef("books__id") joins books into the outer query - iterator() pages over the repeated
    rows and a .distinct() count sees them."""
    await create_authors_with_books()
    rating = Subquery(Book.objects.filter(id=OuterRef("books__id")).values("rating"))
    with_ratings = Author.objects.annotate(rating=rating).order_by("id")
    rows = [(author.id, author.rating) for author in await with_ratings]

    assert len(rows) == 5
    assert [(author.id, author.rating) async for author in with_ratings.iterator(chunk_size=1)] == rows
    assert await Author.objects.annotate(rating=rating).distinct().count() == 5
    assert await Author.objects.annotate(rating=rating).count() == 5

    rated = Author.objects.filter(Exists(Book.objects.filter(id=OuterRef("books__id"), rating__gte=2))).order_by("id")
    assert [author.id async for author in rated.iterator(chunk_size=1)] == [author.id for author in await rated]


@pytest.mark.asyncio
async def test_group_by_is_ignored_where_model_instances_become_a_subquery(db):
    """Model instances aren't grouped by .group_by() - nor is the primary key subquery built from
    them for an __in filter, a sliced last() or a get()."""
    first_author, second_author = await create_authors_with_books()
    grouped = Author.objects.annotate(n=Count("books")).group_by("name")

    assert await Book.objects.filter(author__in=grouped).count() == 5
    assert (await grouped.order_by("id")[:2].last()).id == second_author.id
    assert (await grouped.get(id=first_author.id)).id == first_author.id


@pytest.mark.asyncio
async def test_filtering_a_sliced_queryset_is_rejected(db):
    """Like Django - filtering the slice itself would need a subquery, filtering before it would
    change which rows the slice holds."""
    await create_authors_with_books()
    sliced = Book.objects.all().order_by("id")[:2]
    for filtering in (
        lambda: sliced.filter(rating=1.0),
        lambda: sliced.exclude(rating=1.0),
        lambda: sliced.get(rating=1.0),
        lambda: sliced.get_or_none(rating=1.0),
    ):
        with pytest.raises(QueryError, match="Cannot filter a query once a slice has been taken"):
            filtering()


@pytest.mark.asyncio
async def test_reordering_or_distinct_on_a_sliced_queryset_is_rejected(db):
    """Like Django - reordering or deduplicating before the LIMIT would change which rows the
    slice holds."""
    await create_authors_with_books()
    sliced_querysets = (
        Book.objects.all().order_by("id")[:2],
        Book.objects.all().order_by("id").limit(2),
        Book.objects.all().order_by("id").offset(1),
        Book.objects.all().values_list("id", flat=True).order_by("id")[:2],
        Book.objects.all().values("id").order_by("id").limit(2),
        Book.objects.filter(id__gt=0).union(Book.objects.filter(id__gt=0))[:2],
        Book.objects.all()
        .values_list("id", flat=True)
        .union(Book.objects.all().values_list("id", flat=True))
        .limit(2),
    )
    for sliced in sliced_querysets:
        with pytest.raises(QueryError, match="Cannot reorder a query once a slice has been taken"):
            sliced.order_by("-id")
    for sliced in sliced_querysets[:5]:
        with pytest.raises(QueryError, match="Cannot create distinct fields once a slice has been taken"):
            sliced.distinct()
        with pytest.raises(QueryError, match="Cannot create distinct fields once a slice has been taken"):
            sliced.distinct("id")
    assert [book.rating for book in await Book.objects.all().order_by("-rating").limit(2)] == [5.0, 4.0]


@pytest.mark.asyncio
async def test_row_taken_from_a_slice_cannot_be_filtered_or_reordered(db):
    """first()/an index of a slice picks its row from the slice - filtering or reordering it
    afterwards would pick from other rows."""
    await create_authors_with_books()
    ordered = Book.objects.all().order_by("id")
    rows_taken_from_slices = (
        ordered[2:5].first(),
        ordered[:3].first(),
        ordered[2],
        ordered.values("id")[2:5].first(),
    )
    for row_query in rows_taken_from_slices:
        with pytest.raises(QueryError, match="Cannot filter a query once a slice has been taken"):
            row_query.filter(rating=1.0)
        with pytest.raises(QueryError, match="Cannot reorder a query once a slice has been taken"):
            row_query.order_by("-id")
    assert (await ordered[2:5].first()).rating == 3.0
    # first() of an unsliced queryset is narrowed only when fetched.
    assert (await ordered.first().filter(rating__gt=1.0)).rating == 2.0
    assert (await ordered.first().order_by("-rating")).rating == 5.0


@pytest.mark.asyncio
async def test_last_of_an_unordered_slice_is_a_row_of_that_slice(db):
    """An unordered slice is taken by the primary key for first() and last() alike."""
    await create_authors_with_books()
    books_by_id = await Book.objects.all().order_by("id")
    unordered_slice = Book.objects.all()[1:3]

    assert (await unordered_slice.first()).id == books_by_id[1].id
    assert (await unordered_slice.last()).id == books_by_id[2].id
    assert await Book.objects.all().values_list("id", flat=True)[1:3].last() == books_by_id[2].id
    assert (await Book.objects.filter(rating__gte=2.0)[1:3].last()).id == books_by_id[3].id


@pytest.mark.asyncio
async def test_union_query_slices_and_index(db):
    await create_authors_with_books()
    union = Book.objects.filter(rating__lt=3.0).union(Book.objects.filter(rating__gte=4.0)).order_by("rating")

    assert [book.rating for book in await union[1:3]] == [2.0, 4.0]
    assert [book.rating for book in await union[1:][1:2]] == [4.0]
    assert [book.rating for book in await union.limit(3)[1:]] == [2.0, 4.0]
    assert (await union[3]).rating == 5.0
    with pytest.raises(IndexError, match="Index 4 is out of range"):
        await union[4]
    with pytest.raises(QueryError, match="Negative indexing is not supported"):
        union[-1]
    with pytest.raises(QueryError, match="Slice steps should be 1 or None"):
        union[::2]


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_get_on_distinct_on_filters_before_picking_rows(db):
    """get(**kwargs) is filter(**kwargs).get() - the condition applies before DISTINCT ON picks a
    row per group, as filter() does."""
    await create_authors_with_books()
    per_author = Book.objects.all().order_by("author_id", "-rating").distinct("author_id")

    assert (await per_author.get(rating=2.0)).rating == 2.0
    assert (await per_author.get_or_none(rating=4.0)).rating == 4.0
    assert (await per_author.filter(rating=2.0).get()).rating == 2.0


@pytest.mark.asyncio
async def test_unused_alias_adds_no_join(db):
    """An .alias() nothing reads is left out of the query - its to-many JOIN doesn't repeat rows."""
    await create_authors_with_books()
    aliased = Author.objects.all().alias(book_name=F("books__name"))

    assert len(await aliased) == 2
    assert await aliased.count() == 2
    assert await aliased.exists() is True
    assert len([author async for author in aliased.order_by("id").iterator(chunk_size=1)]) == 2
    assert len(await aliased.values_list("id", flat=True)) == 2
    assert await aliased.annotate(n=Count("books")).order_by("id").values_list("n", flat=True) == [3, 2]
    assert await aliased.filter(book_name="book 1.0").count() == 1
    assert len(await aliased.values_list("id", "book_name")) == 5
