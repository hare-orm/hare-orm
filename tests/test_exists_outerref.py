import pytest
import pytest_asyncio

from hare.exceptions import (
    FieldError,
    QueryError,
)
from hare.query.expressions import Exists, F, OuterReference, Q, Subquery, Window
from hare.query.functions import Coalesce, Count, Length, Sum
from hare.query.functions.window import RowNumber
from tests.testmodels import Address, Author, Book, Employee, Event, Tournament


@pytest.mark.asyncio
async def test_exists_outerref_filters_correlated(db):
    has_high_rated = await Author.objects.create(name="HasHighRated")
    no_high_rated = await Author.objects.create(name="NoHighRated")
    no_books = await Author.objects.create(name="NoBooks")
    await Book.objects.create(name="b1", author=has_high_rated, rating=5.0)
    await Book.objects.create(name="b2", author=has_high_rated, rating=1.0)
    await Book.objects.create(name="b3", author=no_high_rated, rating=1.0)

    results = (
        await Author.objects.all()
        .annotate(has_high_rated=Exists(Book.objects.filter(author_id=OuterReference("id"), rating__gte=4.0)))
        .filter(has_high_rated=True)
    )
    assert [a.id for a in results] == [has_high_rated.id]

    results_not = (
        await Author.objects.all()
        .annotate(has_high_rated=Exists(Book.objects.filter(author_id=OuterReference("id"), rating__gte=4.0)))
        .filter(~Q(has_high_rated=True))
    )
    assert sorted(a.id for a in results_not) == sorted([no_high_rated.id, no_books.id])


@pytest.mark.asyncio
async def test_exists_negated_renders_not_exists(db):
    """~Exists(...) used to raise TypeError (no __invert__ at all, unlike Q) - the natural way to
    express "no related row matches" directly on an Exists() annotation, without going through
    ~Q(annotation=True)/exclude() (already covered by test_exists_outerref_filters_correlated
    above). Must produce the same NOT EXISTS semantics."""
    has_high_rated = await Author.objects.create(name="HasHighRated")
    no_high_rated = await Author.objects.create(name="NoHighRated")
    no_books = await Author.objects.create(name="NoBooks")
    await Book.objects.create(name="b1", author=has_high_rated, rating=5.0)
    await Book.objects.create(name="b2", author=has_high_rated, rating=1.0)
    await Book.objects.create(name="b3", author=no_high_rated, rating=1.0)

    negated = ~Exists(Book.objects.filter(author_id=OuterReference("id"), rating__gte=4.0))
    assert negated.negated is True
    assert (~negated).negated is False

    results = await Author.objects.all().annotate(has_high_rated=negated).filter(has_high_rated=True)
    assert sorted(a.id for a in results) == sorted([no_high_rated.id, no_books.id])


@pytest.mark.asyncio
async def test_exists_outerref_combines_with_regular_filter(db):
    author = await Author.objects.create(name="Real")
    other = await Author.objects.create(name="Other")
    await Book.objects.create(name="b1", author=author, rating=5.0)
    await Book.objects.create(name="b2", author=other, rating=5.0)

    results = (
        await Author.objects.filter(name="Real")
        .annotate(has_rated=Exists(Book.objects.filter(author_id=OuterReference("id"), rating__gte=1.0)))
        .filter(has_rated=True)
    )
    assert [a.id for a in results] == [author.id]


@pytest.mark.asyncio
async def test_exists_outerref_qualifies_ambiguous_shared_column_name(db):
    """Author and Book both have an "id" - without explicit table qualification, OuterReference("id")
    used to silently resolve to the child table's (Book) id instead of the outer one (Author) -
    regression test for exactly this bug (found and fixed during development)."""
    a1 = await Author.objects.create(name="A1")
    a2 = await Author.objects.create(name="A2")
    await Book.objects.create(name="b1", author=a1, rating=5.0)
    await Book.objects.create(name="b2", author=a2, rating=1.0)

    results = (
        await Author.objects.all()
        .annotate(has_book=Exists(Book.objects.filter(author_id=OuterReference("id"))))
        .filter(has_book=True)
    )
    assert sorted(a.id for a in results) == sorted([a1.id, a2.id])


def test_outerref_outside_exists_raises():
    with pytest.raises(QueryError):
        OuterReference("id").get_result(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_outerref_unknown_field_raises(db):
    with pytest.raises(FieldError):
        await (
            Author.objects.all()
            .annotate(x=Exists(Book.objects.filter(author_id=OuterReference("not_a_real_field"))))
            .filter(x=True)
        )


@pytest.mark.asyncio
async def test_exists_outerref_combines_with_prefetch_related_on_outer_query(db):
    """_make_query() is synchronous (no await) - the window where outer_expression_context is set
    never overlaps with prefetch's async execution (a separate query AFTER the main SELECT), so
    prefetch on the outer query can't see/corrupt someone else's OuterReference context."""
    author = await Author.objects.create(name="A1")
    other = await Author.objects.create(name="A2")
    await Book.objects.create(name="b1", author=author, rating=5.0)
    await Book.objects.create(name="b2", author=author, rating=1.0)
    await Book.objects.create(name="b3", author=other, rating=1.0)

    results = (
        await Author.objects.all()
        .prefetch_related("books")
        .annotate(has_high_rated=Exists(Book.objects.filter(author_id=OuterReference("id"), rating__gte=4.0)))
        .filter(has_high_rated=True)
    )
    assert len(results) == 1 and results[0].id == author.id
    assert len(results[0].books) == 2


@pytest.mark.asyncio
async def test_exists_outerref_self_referential_model(db):
    """OuterReference/Exists() used to lose the correlation entirely when the child queryset's model
    is the SAME as the outer query's model (a self-referential FK like Employee.manager, or any
    other reuse of the same model at two nesting levels). The child's own unaliased FROM clause
    renders the exact same table name as the outer scope, so per standard SQL name resolution,
    OuterReference("id")'s bare "employee"."id" reference bound to the child's OWN nearest-enclosing
    scope instead of the actual outer row - silently, with no error at all (the query still ran,
    it just always compared a row to itself instead of to the real outer row)."""
    e1 = await Employee.objects.create(name="e1")
    e2 = await Employee.objects.create(name="e2", manager=e1)
    e3 = await Employee.objects.create(name="e3", manager=e2)

    results = (
        await Employee.objects.all()
        .annotate(has_report=Exists(Employee.objects.filter(manager_id=OuterReference("id"))))
        .filter(has_report=True)
    )
    assert sorted(e.id for e in results) == sorted([e1.id, e2.id])
    assert e3.id not in [e.id for e in results]


@pytest.mark.asyncio
async def test_exists_outerref_self_referential_model_nested_two_levels(db):
    """Same root cause as test_exists_outerref_self_referential_model, but two Exists() levels
    deep, reusing the same model at both levels - doesn't even require a literal self-referential
    FK, just the same model appearing twice in nested correlated subqueries."""
    e1 = await Employee.objects.create(name="e1")
    e2 = await Employee.objects.create(name="e2", manager=e1)
    await Employee.objects.create(name="e3", manager=e2)  # e2's report (e3) has no report of its own

    results = (
        await Employee.objects.all()
        .annotate(
            has_report_with_report=Exists(
                Employee.objects.filter(manager_id=OuterReference("id"))
                .annotate(has_own_report=Exists(Employee.objects.filter(manager_id=OuterReference("id"))))
                .filter(has_own_report=True)
            )
        )
        .filter(has_report_with_report=True)
    )
    # e1's report (e2) itself has a report (e3) -> e1 matches.
    # e2's report (e3) has no report of its own -> e2 does not match.
    assert [e.id for e in results] == [e1.id]


@pytest.mark.asyncio
async def test_exists_outerref_with_chained_inner_queryset(db):
    """Exists(...) doesn't have to receive a "bare" filter() - regular QuerySet chains
    (exclude/filter/order_by) on the child query must keep working as usual."""
    author = await Author.objects.create(name="A1")
    await Book.objects.create(name="b1", author=author, rating=5.0)
    await Book.objects.create(name="b2", author=author, rating=1.0)

    results = (
        await Author.objects.all()
        .annotate(
            has_non_b1=Exists(Book.objects.filter(author_id=OuterReference("id")).exclude(name="b1").order_by("name"))
        )
        .filter(has_non_b1=True)
    )
    assert [a.id for a in results] == [author.id]


@pytest.mark.asyncio
async def test_outerref_related_field_path(db):
    """OuterReference("related__field") references a field reachable through a relation on the OUTER
    model - the same way .filter("related__field=...") already can. The required JOIN must be
    added to the OUTER query (Book), not the child one (Tournament)."""
    a1 = await Author.objects.create(name="Smith")
    a2 = await Author.objects.create(name="Jones")
    b1 = await Book.objects.create(name="X", author=a1, rating=5.0)
    b2 = await Book.objects.create(name="Y", author=a2, rating=1.0)
    await Tournament.objects.create(name="Smith")

    results = (
        await Book.objects.all()
        .annotate(author_is_famous=Exists(Tournament.objects.filter(name=OuterReference("author__name"))))
        .filter(author_is_famous=True)
    )
    assert [b.id for b in results] == [b1.id]

    results_not = (
        await Book.objects.all()
        .annotate(author_is_famous=Exists(Tournament.objects.filter(name=OuterReference("author__name"))))
        .filter(~Q(author_is_famous=True))
    )
    assert [b.id for b in results_not] == [b2.id]


@pytest.mark.asyncio
async def test_outerref_related_field_path_two_hops(db):
    """OuterReference() supports a path through MULTIPLE relations, not just one - each hop adds its
    own JOIN to the outer query, in the same order as a regular .filter() lookup path."""
    t1 = await Tournament.objects.create(name="T1")
    t2 = await Tournament.objects.create(name="T2")
    e1 = await Event.objects.create(name="E1", tournament=t1)
    e2 = await Event.objects.create(name="E2", tournament=t2)
    addr1 = await Address.objects.create(city="C1", street="S1", event=e1)
    await Address.objects.create(city="C2", street="S2", event=e2)
    await Author.objects.create(name="T1")

    results = (
        await Address.objects.all()
        .annotate(matches=Exists(Author.objects.filter(name=OuterReference("event__tournament__name"))))
        .filter(matches=True)
    )
    assert [a.event_id for a in results] == [addr1.event_id]


@pytest.mark.asyncio
async def test_outerref_related_field_path_unknown_field_raises(db):
    with pytest.raises(FieldError):
        await (
            Book.objects.all()
            .annotate(x=Exists(Tournament.objects.filter(name=OuterReference("author__not_a_field"))))
            .filter(x=True)
        )


@pytest.mark.asyncio
async def test_outerref_related_field_path_nested_inside_exclude(db):
    """Regression test for join retargeting: when OuterReference("related__field") sits inside
    Exists(...), which is itself part of a SINGLE Q node that exclude() rewrites into a
    correlated NOT EXISTS (Q._negate_across_joins, because another kwarg of that same Q crosses
    its own relation) - the result must stay correct."""
    a1 = await Author.objects.create(name="Smith")
    a2 = await Author.objects.create(name="Jones")
    await Book.objects.create(name="X1", author=a1, rating=5.0)
    await Book.objects.create(name="X2", author=a1, rating=1.0)
    b3 = await Book.objects.create(name="Y1", author=a2, rating=1.0)
    await Tournament.objects.create(name="Smith")

    results = (
        await Book.objects.all()
        .annotate(author_is_famous=Exists(Tournament.objects.filter(name=OuterReference("author__name"))))
        .exclude(Q(author__name="Smith", author_is_famous=True))
    )
    assert sorted(b.id for b in results) == [b3.id]


@pytest.mark.asyncio
async def test_outerref_simple_field_nested_inside_exclude(db):
    """Same retargeting, for a PLAIN (non-related__) OuterReference - its QualifiedOuterField.table
    is literally the OUTER (replaceable) table, unlike the related__ case (where the table is
    already joined and aliased on its own). Verifies that ExistsTerm.replace_table() propagates
    the retargeting into the child query rather than silently leaving it untouched (the same bug
    class already found and fixed for Extract in hare/sql/functions.py)."""
    a1 = await Author.objects.create(name="Smith")
    a2 = await Author.objects.create(name="Jones")
    await Book.objects.create(name="X1", author=a1, rating=5.0)
    await Book.objects.create(name="X2", author=a1, rating=1.0)
    b3 = await Book.objects.create(name="Y1", author=a2, rating=1.0)

    results = (
        await Book.objects.all()
        .annotate(has_self=Exists(Book.objects.filter(id=OuterReference("id"))))
        .exclude(Q(author__name="Smith", has_self=True))
    )
    assert sorted(b.id for b in results) == [b3.id]


@pytest.mark.asyncio
async def test_subquery_outerref_scalar_annotation(db):
    """OuterReference() inside a bare Subquery(...) - not wrapped in Exists(...) - used to raise
    ConfigurationError unconditionally: Subquery never set outer_expression_context the way Exists
    does, so a correlated scalar subquery (the classic Subquery(OuterReference(...)) pattern, distinct
    from Exists's boolean-only correlation) had no way to reach the outer row at all. Regression
    test for the fix."""
    author = await Author.objects.create(name="A1")
    other = await Author.objects.create(name="A2")
    await Book.objects.create(name="b1", author=author, rating=5.0)
    await Book.objects.create(name="b2", author=author, rating=1.0)
    await Book.objects.create(name="b3", author=other, rating=2.0)

    results = (
        await Author.objects.all()
        .annotate(
            total_rating=Subquery(
                Book.objects.filter(author_id=OuterReference("id"))
                .values("author_id")
                .annotate(total=Sum("rating"))
                .values("total")
            )
        )
        .order_by("id")
    )
    ratings_by_author = {a.id: a.total_rating for a in results}
    assert ratings_by_author[author.id] == pytest.approx(6.0)
    assert ratings_by_author[other.id] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_subquery_outerref_coalesce_wrapped(db):
    """Same correlated-scalar-subquery pattern wrapped in Coalesce(...) - covers an author with
    no matching book getting the default instead of NULL, and exercises the resolution path
    through Function._get_argument() (already Expression-first) rather than _get_annotate()'s
    own Term-vs-Expression check directly."""
    has_books = await Author.objects.create(name="HasBooks")
    no_books = await Author.objects.create(name="NoBooks")
    await Book.objects.create(name="b1", author=has_books, rating=3.0)

    results = (
        await Author.objects.all()
        .annotate(
            total_rating=Coalesce(
                Subquery(
                    Book.objects.filter(author_id=OuterReference("id"), rating__gte=1.0)
                    .annotate(total=Sum("rating"))
                    .values("total")
                ),
                0,
            )
        )
        .order_by("id")
    )
    ratings_by_author = {a.id: a.total_rating for a in results}
    assert ratings_by_author[has_books.id] == pytest.approx(3.0)
    assert ratings_by_author[no_books.id] == 0


@pytest.mark.asyncio
async def test_subquery_outerref_as_filter_value(db):
    """A bare Subquery(...OuterReference...) used directly as a filter value (not an annotation) -
    covers the isinstance(Expression) resolution path in Q._get_actual_filter_params(), a
    separate call site from _get_annotate()'s own."""
    author = await Author.objects.create(name="TopRated")
    other = await Author.objects.create(name="Other")
    await Book.objects.create(name="b1", author=author, rating=9.0)
    await Book.objects.create(name="b2", author=other, rating=1.0)

    results = await Author.objects.filter(
        id__in=Subquery(Book.objects.filter(author_id=OuterReference("id"), rating__gte=5.0).values("author_id"))
    )
    assert [a.id for a in results] == [author.id]


@pytest.mark.asyncio
async def test_outerref_supports_arithmetic(db):
    """OuterReference() had no `+`/`-`/... dunder methods of its own (unlike F(), which defines them
    directly on itself) - `OuterReference("x") + 1` raised a raw TypeError instead of building a
    CombinedExpression, since Expression (both F's and OuterReference's common base) didn't provide
    them either. Moved onto Expression itself so every Expression subclass gets arithmetic
    support, not just F()."""
    author = await Author.objects.create(name="A1")
    await Book.objects.create(name="b1", author=author, rating=5.0)

    results = await Author.objects.all().annotate(
        has_match=Exists(Book.objects.filter(author_id=OuterReference("id") + 0, rating__gte=1.0))
    )
    assert [a.has_match for a in results]


@pytest.mark.asyncio
async def test_exists_annotation_decodes_to_real_bool(db):
    """An Exists() annotation's raw driver value used to leak through undecoded: SQLite's own
    EXISTS(...) has no boolean type and returns a plain integer 0/1, unlike Postgres, whose
    driver already decodes it into a real bool - so `type(instance.has_events)` differed across
    backends even though `instance.has_events == True/False` happened to hold either way (1 == True
    in Python). Must be a real bool on every backend, both for a plain model instance attribute
    and through .values()."""
    tournament_with_events = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=tournament_with_events)
    await Tournament.objects.create(name="T2")

    annotated = await Tournament.objects.annotate(
        has_events=Exists(Event.objects.filter(tournament_id=OuterReference("id")))
    ).order_by("name")
    assert [(t.name, t.has_events, type(t.has_events)) for t in annotated] == [
        ("T1", True, bool),
        ("T2", False, bool),
    ]

    values_rows = (
        await Tournament.objects.annotate(has_events=Exists(Event.objects.filter(tournament_id=OuterReference("id"))))
        .order_by("name")
        .values("name", "has_events")
    )
    assert [(row["name"], row["has_events"], type(row["has_events"])) for row in values_rows] == [
        ("T1", True, bool),
        ("T2", False, bool),
    ]


@pytest.mark.asyncio
async def test_nested_outerref_raises_clear_error():
    """OuterReference(OuterReference(...)) - nesting one OuterReference inside another - isn't supported (there's
    only ever one enclosing outer query, tracked by a single contextvar slot, not a stack of
    them); used to raise a confusing AttributeError deep inside field-name string handling
    instead of a clear, immediate error naming the actual problem."""
    with pytest.raises(QueryError):
        OuterReference(OuterReference("id"))


@pytest.mark.asyncio
async def test_outerref_forward_relation_name_means_its_key_column(db):
    author = await Author.objects.create(name="first")
    await Author.objects.create(name="second")
    await Book.objects.create(name="book", author=author, rating=1)
    author_names = await Book.objects.annotate(
        author_name=Subquery(Author.objects.filter(id=OuterReference("author")).values_list("name", flat=True))
    ).values_list("author_name", flat=True)
    assert author_names == ["first"]
    has_author = await Book.objects.annotate(
        has_author=Exists(Author.objects.filter(id=OuterReference("author") + 0))
    ).values_list("has_author", flat=True)
    assert has_author == [True]


@pytest.mark.asyncio
async def test_outerref_resolves_outer_query_annotation(db):
    author = await Author.objects.create(name="first")
    await Author.objects.create(name="second")
    await Book.objects.create(name="book", author=author, rating=5)
    has_books = await (
        Author.objects.annotate(author_id_copy=F("id") + 0)
        .annotate(has_books=Exists(Book.objects.filter(author_id=OuterReference("author_id_copy"))))
        .order_by("name")
        .values_list("has_books", flat=True)
    )
    assert has_books == [True, False]
    long_named = await (
        Author.objects.annotate(name_length=Length("name"))
        .annotate(
            has_better_book=Exists(
                Book.objects.filter(author_id=OuterReference("id"), rating__lt=OuterReference("name_length"))
            )
        )
        .order_by("name")
        .values_list("has_better_book", flat=True)
    )
    assert long_named == [False, False]


@pytest.mark.asyncio
async def test_self_referential_subquery_with_join_orders_by_its_own_rows(db):
    """A self-referential Subquery whose own JOIN makes it table-qualify its columns ordered by
    the OUTER table's column - a constant on Postgres (an arbitrary row picked by LIMIT 1), an
    unknown column on SQLite."""
    boss = await Employee.objects.create(name="boss")
    await Employee.objects.create(name="first", manager=boss)
    await Employee.objects.create(name="second", manager=boss)

    newest_report = (
        Employee.objects.filter(manager_id=OuterReference("id"), manager__name__isnull=False)
        .order_by("-id")
        .limit(1)
        .values("name")
    )
    oldest_report = (
        Employee.objects.filter(manager_id=OuterReference("id"), manager__name__isnull=False)
        .order_by("id")
        .limit(1)
        .values("name")
    )

    assert await Employee.objects.filter(id=boss.id).annotate(report=Subquery(newest_report)).values_list(
        "report", flat=True
    ) == ["second"]
    assert await Employee.objects.filter(id=boss.id).annotate(report=Subquery(oldest_report)).values_list(
        "report", flat=True
    ) == ["first"]


@pytest_asyncio.fixture
async def authors_with_book_counts(db):
    """Authors with 2, 1 and 0 books, and every book rated 1..3."""
    two_books = await Author.objects.create(name="two")
    one_book = await Author.objects.create(name="one")
    no_books = await Author.objects.create(name="none")
    await Book.objects.create(name="a", author=two_books, rating=1)
    await Book.objects.create(name="b", author=two_books, rating=3)
    await Book.objects.create(name="c", author=one_book, rating=2)
    return two_books, one_book, no_books


def _authors_with_cheaper_book_flag():
    """Each author's book count, and whether any book is rated below that count."""
    return Author.objects.annotate(book_count=Count("books")).annotate(
        has_book_rated_below_count=Exists(Book.objects.filter(rating__lt=OuterReference("book_count")))
    )


@pytest.mark.asyncio
async def test_outerref_to_aggregate_annotation_is_not_grouped_by(authors_with_book_counts):
    """Exists(... OuterReference(<aggregate annotation>) ...) inlines the aggregate - it used to be
    copied into the outer GROUP BY, which every backend rejects."""
    two_books, one_book, no_books = authors_with_book_counts

    rows = (
        await _authors_with_cheaper_book_flag()
        .order_by("id")
        .values_list("id", "book_count", "has_book_rated_below_count")
    )
    authors = await _authors_with_cheaper_book_flag().order_by("id")

    assert rows == [(two_books.id, 2, True), (one_book.id, 1, False), (no_books.id, 0, False)]
    assert [(author.id, author.has_book_rated_below_count) for author in authors] == [
        (two_books.id, True),
        (one_book.id, False),
        (no_books.id, False),
    ]


@pytest.mark.asyncio
async def test_outerref_to_aggregate_annotation_filters_in_having(authors_with_book_counts):
    """A filter on such an Exists/Subquery annotation is a HAVING condition, like one on the
    aggregate itself."""
    two_books, one_book, no_books = authors_with_book_counts
    highest_rating_below_count = (
        Book.objects.filter(rating__lt=OuterReference("book_count")).order_by("-rating").limit(1).values("rating")
    )

    flagged = await _authors_with_cheaper_book_flag().filter(has_book_rated_below_count=True)
    not_flagged = (
        await Author.objects.annotate(book_count=Count("books"))
        .annotate(has_book_rated_below_count=~Exists(Book.objects.filter(rating__lt=OuterReference("book_count"))))
        .filter(has_book_rated_below_count=True)
    )
    with_rating = (
        await Author.objects.annotate(book_count=Count("books"))
        .annotate(rating_below_count=Subquery(highest_rating_below_count))
        .filter(rating_below_count__gte=1)
        .values_list("id", "rating_below_count")
    )

    assert [author.id for author in flagged] == [two_books.id]
    assert sorted(author.id for author in not_flagged) == sorted([one_book.id, no_books.id])
    assert with_rating == [(two_books.id, 1.0)]
    assert await _authors_with_cheaper_book_flag().filter(has_book_rated_below_count=True).count() == 1


@pytest.mark.asyncio
async def test_outerref_to_window_annotation_raises(db):
    """A window function can't be evaluated inside a subquery of the query computing it."""
    queryset = Author.objects.annotate(position=Window(RowNumber(), order_by=["id"])).annotate(
        has_book=Exists(Book.objects.filter(rating__lt=OuterReference("position")))
    )

    with pytest.raises(QueryError, match="window function"):
        await queryset
