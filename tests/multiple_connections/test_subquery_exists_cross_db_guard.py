import pytest

from hare.contrib.test import capture_queries
from hare.exceptions import (
    QueryError,
)
from hare.query.expressions import Exists, F, OuterReference, RawSQL, Subquery, Value
from hare.transactions import Transactions
from tests.shared_models.multi_database_models import Author, Book
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.statement_plan_checks import StatementPlanChecks


@pytest.mark.asyncio
async def test_exists_using_db_different_from_outer_raises(two_databases):
    """Regression test: Exists(...) wrapping a queryset explicitly pinned (.using()) to a
    DIFFERENT physical connection than the outer query used to silently ignore that pin and run
    the whole compiled SQL text (including the wrapped SELECT) against the OUTER connection only
    - never erroring, just quietly matching nothing whenever the two connections' data actually
    differs. Must now raise ConfigurationError at query-build time instead."""
    db, second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    with pytest.raises(QueryError, match="different database connection"):
        await (
            Author.objects.all()
            .annotate(has_book=Exists(Book.objects.filter(author_id=OuterReference("id")).using(second_db)))
            .filter(has_book=True)
        )


@pytest.mark.asyncio
async def test_subquery_using_db_different_from_outer_raises(two_databases):
    """Same cross-connection guard, for a bare Subquery(...) annotation rather than Exists(...)."""
    db, second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    with pytest.raises(QueryError, match="different database connection"):
        await Author.objects.all().annotate(
            book_id=Subquery(Book.objects.filter(author_id=OuterReference("id")).using(second_db).values("id"))
        )


@pytest.mark.asyncio
async def test_exists_without_explicit_using_db_still_works(two_databases):
    """No explicit .using() on the wrapped queryset - it resolves through the router to the
    SAME connection as the outer query (both Author and Book default to app "models") - this
    legitimate, un-pinned case must keep working exactly as before, even with a second,
    unrelated connection alive in the same context."""
    db, _second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    other = await Author.objects.using(db).create(name="A2")
    await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    results = (
        await Author.objects.all()
        .using(db)
        .annotate(has_book=Exists(Book.objects.filter(author_id=OuterReference("id"))))
        .filter(has_book=True)
    )
    assert [a.id for a in results] == [author.id]
    assert other.id not in [a.id for a in results]


@pytest.mark.asyncio
async def test_pinned_exists_is_refused_after_the_same_query_kept_a_plan(two_databases):
    """The same query run unpinned first keeps a statement plan; the pinned one must not run on
    that plan, which would skip the check of the wrapped queryset's connection."""
    db, second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    unpinned = await (
        Author.objects.all()
        .annotate(has_book=Exists(Book.objects.filter(author_id=OuterReference("id"))))
        .filter(has_book=True)
    )
    assert [found.id for found in unpinned] == [author.id]

    with pytest.raises(QueryError, match="different database connection"):
        await (
            Author.objects.all()
            .annotate(has_book=Exists(Book.objects.filter(author_id=OuterReference("id")).using(second_db)))
            .filter(has_book=True)
        )


@pytest.mark.asyncio
async def test_pinned_subquery_is_refused_after_the_same_query_kept_a_plan(two_databases):
    """Same for a Subquery(...) annotation."""
    db, second_db = two_databases
    author = await Author.objects.using(db).create(name="A1")
    book = await Book.objects.using(db).create(name="b1", author=author, rating=5.0)

    unpinned = await Author.objects.all().annotate(
        book_id=Subquery(Book.objects.filter(author_id=OuterReference("id")).values("id"))
    )
    assert [found.book_id for found in unpinned] == [book.id]

    with pytest.raises(QueryError, match="different database connection"):
        await Author.objects.all().annotate(
            book_id=Subquery(Book.objects.filter(author_id=OuterReference("id")).using(second_db).values("id"))
        )


@pytest.mark.asyncio
async def test_embedded_queries_refuse_different_clients_sharing_an_alias(two_databases, tmp_path):
    shared, second = two_databases
    if DatabaseUnderTest.get_dialect().name == "sqlite":
        independent = shared.create_independent_client({"file_path": str(tmp_path / "independent.sqlite3")})
    else:
        independent = shared.create_independent_client({"database": second.database})
    try:
        await independent.generate_schema(safe=True)
        left = await Author.objects.using(shared).create(name="left")
        right = await Author.objects.using(independent).create(name="right")
        assert right.pk == left.pk
        assert await Author.objects.using(independent).values_list("name", flat=True) == ["right"]

        def embedded(source, selected):
            return (
                source.annotate(found=Exists(selected)).values_list("found", flat=True),
                source.annotate(text=Subquery(selected.values("name")[:1])).values_list("text", flat=True),
                source.filter(id__in=selected.values("id")).values_list("name", flat=True),
                source.with_cte("selected", selected.values("id"))
                .filter(id__in=RawSQL("SELECT id FROM selected"))
                .values_list("name", flat=True),
                source.values_list("name", flat=True).union(selected.values_list("name", flat=True)),
            )

        source = Author.objects.using(shared)

        async def assert_foreign_queries_refused():
            for query in embedded(source, Author.objects.using(independent)):
                async with capture_queries(using=shared) as queries:
                    with pytest.raises(QueryError, match="different database connection"):
                        await query
                assert queries.count == 0

        await assert_foreign_queries_refused()
        local_expected = ([True], ["left"], ["left"], ["left"], ["left"])
        nonmatching_expected = ([False], [None], [], [], ["left"])
        for query, expected in zip(embedded(source, source), local_expected, strict=True):
            assert await query == expected
        warmed = embedded(source, Author.objects.filter(name="left"))
        later = embedded(source, Author.objects.filter(name="right"))
        plan_hits = []
        for first, second_query, first_rows, second_rows in zip(
            warmed, later, local_expected, nonmatching_expected, strict=True
        ):
            assert await first == first_rows
            rows, hits = await StatementPlanChecks.count_plan_hits(second_query)
            assert rows == second_rows
            plan_hits.append(hits)
        # The first four forms keep plans. UNION checks its inputs during a full build.
        assert plan_hits[:4] == [1, 1, 1, 1]
        await assert_foreign_queries_refused()

        connection_source = Author.objects.using(independent).values("name", inserted_id=F("id") + 10)
        async with capture_queries(using=shared) as queries:
            with pytest.raises(QueryError, match="different database connection"):
                await source.insert_from(connection_source, fields=("name", "id"))
        assert queries.count == 0
        assert await source.order_by("id").values_list("id", "name") == [(left.pk, "left")]
        same_connection_source = source.values("name", inserted_id=F("id") + 10)
        assert await source.insert_from(same_connection_source, fields=("name", "id")) == 1
        assert await source.order_by("id").values_list("id", "name") == [(left.pk, "left"), (left.pk + 10, "left")]

        if shared.features.supports_merge:
            external = Author.objects.using(independent).values("id", "name")
            async with capture_queries(using=shared) as queries:
                with pytest.raises(QueryError, match="different database connection"):
                    await source.merge(external, on="id").when_matched(update={"name": F("merge_source__name")})
            assert queries.count == 0
            local = source.filter(id=left.pk).values("id", updated_name=Value("left suffix"))
            assert (
                await source.merge(local, on="id").when_matched(update={"name": F("merge_source__updated_name")}) == 1
            )
            assert await source.order_by("id").values_list("id", "name") == [
                (left.pk, "left suffix"),
                (left.pk + 10, "left"),
            ]

        async with Transactions.atomic(using=shared.connection_alias) as transaction:
            transaction_source = Author.objects.using(transaction).filter(id=left.pk)
            assert await transaction_source.annotate(found=Exists(source)).values_list("found", flat=True) == [True]
    finally:
        await independent.close()
