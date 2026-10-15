import pytest

from tests.testmodels import Author, Book


@pytest.mark.asyncio
async def test_unawaited_queryset_used_as_subquery_in_filter(db):
    """Django-style: a QuerySet passed as a filter value WITHOUT await is used as a subquery
    (field__in=Other.objects.filter(...)) - no need to wrap it in Subquery(...) by hand."""
    has_high_rated = await Author.objects.create(name="HasHighRated")
    no_high_rated = await Author.objects.create(name="NoHighRated")
    await Book.objects.create(name="b1", author=has_high_rated, rating=5.0)
    await Book.objects.create(name="b2", author=no_high_rated, rating=1.0)

    results = await Author.objects.filter(
        id__in=Book.objects.filter(rating__gte=4.0).values_list("author_id", flat=True)
    )
    assert [a.id for a in results] == [has_high_rated.id]


@pytest.mark.asyncio
async def test_unawaited_queryset_combines_with_regular_filter(db):
    active = await Author.objects.create(name="Active")
    await Book.objects.create(name="b1", author=active, rating=5.0)
    inactive_but_high_rated = await Author.objects.create(name="InactiveButHighRated")
    await Book.objects.create(name="b2", author=inactive_but_high_rated, rating=5.0)

    results = await Author.objects.filter(
        name="Active",
        id__in=Book.objects.filter(rating__gte=4.0).values_list("author_id", flat=True),
    )
    assert [a.id for a in results] == [active.id]


@pytest.mark.asyncio
async def test_unawaited_queryset_as_scalar_equality_subquery(db):
    """Not just __in= - any operator accepts a QuerySet as its value (equality here, implying
    exactly one row/column in the subquery's result)."""
    author = await Author.objects.create(name="Solo")
    await Book.objects.create(name="only-book", author=author, rating=3.0)

    results = await Author.objects.filter(id=Book.objects.filter(name="only-book").values_list("author_id", flat=True))
    assert [a.id for a in results] == [author.id]


@pytest.mark.asyncio
async def test_distinct_subquery_ordered_by_unselected_field_with_limit(db):
    # The first occurrence of each name in the ordering is kept in SQL, so the subquery's
    # DISTINCT and LIMIT apply inside the enclosing query too.
    for name in ("a", "a", "b", "c"):
        await Author.objects.create(name=name)
    inner = Author.objects.all().distinct().order_by("-id").limit(2).values_list("name", flat=True)
    assert await inner == ["c", "b"]
    assert sorted(author.name for author in await Author.objects.filter(name__in=inner)) == ["b", "c"]


@pytest.mark.asyncio
async def test_distinct_subquery_ordered_by_unselected_field_without_limit_still_works(db):
    for name in ("a", "a", "b"):
        await Author.objects.create(name=name)
    inner = Author.objects.all().distinct().order_by("-id").values_list("name", flat=True)
    assert len(await Author.objects.filter(name__in=inner)) == 3


@pytest.mark.asyncio
async def test_bare_queryset_without_values_list_is_narrowed_to_pk(db):
    # A bare QuerySet selects every field, not the single column a filter value needs - this
    # used to reach the database as-is and fail with a raw, dialect-specific "expected 1 column"
    # error instead of the actionable narrowing Django itself applies for this exact shape.
    active = await Author.objects.create(name="Active")
    await Author.objects.create(name="Other")

    results = await Author.objects.filter(id__in=Author.objects.filter(name="Active"))
    assert [a.id for a in results] == [active.id]
