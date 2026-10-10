"""values()/values_list(), count() and exists() find their plan before resolving the selected
fields and before checking whether their shape can keep one - a plan found is run, anything the
key doesn't hold still decides by the full check."""

import pytest

from hare.query.expressions import Q
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.relation_loading.select import Select
from tests.testmodels import Author, Book, DoubleFK


async def count_plan_hits(statement) -> tuple[object, int]:
    hits = StatementPlans.hits
    result = await statement
    return result, StatementPlans.hits - hits


async def create_books() -> tuple[Book, Book]:
    first = await Book.objects.create(name="alpha", author=await Author.objects.create(name="a"), rating=1.0)
    second = await Book.objects.create(name="beta", author=await Author.objects.create(name="b"), rating=2.0)
    return first, second


@pytest.mark.asyncio
async def test_values_and_values_list_run_on_their_plan(db):
    first, second = await create_books()
    await Book.objects.filter(name="alpha").values_list("id", flat=True)
    ids, hits = await count_plan_hits(Book.objects.filter(name="beta").values_list("id", flat=True))
    assert (ids, hits) == ([second.id], 1)
    await Book.objects.filter(name="alpha").values("id", "name")
    rows, hits = await count_plan_hits(Book.objects.filter(name="beta").values("id", "name"))
    assert (rows, hits) == ([{"id": second.id, "name": "beta"}], 1)
    rows, hits = await count_plan_hits(Book.objects.filter(name="alpha").values("id", "name"))
    assert (rows, hits) == ([{"id": first.id, "name": "alpha"}], 1)


@pytest.mark.asyncio
async def test_values_across_a_relation_and_of_an_annotation(db):
    await create_books()
    for name, author_name in (("alpha", "a"), ("beta", "b")):
        assert await Book.objects.filter(name=name).values_list("author__name", flat=True) == [author_name]
        assert await Book.objects.filter(name=name).values(writer="author__name") == [{"writer": author_name}]


@pytest.mark.asyncio
async def test_values_selecting_a_field_named_like_a_later_annotation_alias(db):
    """The selected annotations are registered under their aliases, in order, before the key is
    worked out - a field selected before an alias of the same name still selects the field."""
    first, _ = await create_books()
    for _ in range(2):
        rows = await Book.objects.filter(id=first.id).values(name_copy="name", name="author__name")
        assert rows == [{"name_copy": "alpha", "name": "a"}]


@pytest.mark.asyncio
async def test_extra_condition_never_runs_on_a_plan_of_the_same_shape_without_it(db):
    """A select_related() extra condition is not part of the key: the same shape without one
    keeps a plan, which a query with one must not run on."""
    leaf = await DoubleFK.objects.create(name="leaf", left=None)
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)
    for _ in range(2):
        rows = await DoubleFK.objects.filter(pk=root.pk).values("name", "left__left__name")
        assert rows == [{"name": "root", "left__left__name": "leaf"}]
    for extra_condition_name, expected in (("not-leaf", None), ("leaf", "leaf")):
        rows = (
            await DoubleFK.objects.filter(pk=root.pk)
            .select_related("left", Select("left__left", extra_condition=Q(name=extra_condition_name)))
            .values("name", "left__left__name")
        )
        assert rows == [{"name": "root", "left__left__name": expected}]


@pytest.mark.asyncio
async def test_distinct_ordered_by_an_unselected_field_runs_on_its_plan(db):
    """The plan of a .distinct() ordered by a field it doesn't select is the outer query keeping
    the first row of each combination - its filter values and its slice are bound."""
    author_a = await Author.objects.create(name="a")
    author_b = await Author.objects.create(name="b")
    for name, author, rating in (("one", author_a, 1.0), ("two", author_b, 2.0), ("three", author_a, 3.0)):
        await Book.objects.create(name=name, author=author, rating=rating)

    def author_names(minimum_rating):
        return (
            Book.objects.filter(rating__gte=minimum_rating)
            .order_by("rating")
            .values_list("author__name", flat=True)
            .distinct()
        )

    await author_names(0.0)
    for minimum_rating, expected in ((0.0, ["a", "b"]), (2.0, ["b", "a"]), (3.0, ["a"])):
        names, hits = await count_plan_hits(author_names(minimum_rating))
        assert (names, hits) == (expected, 1)
    await author_names(0.0).limit(1)
    for minimum_rating, limit, expected in ((0.0, 1, ["a"]), (2.0, 1, ["b"]), (0.0, 2, ["a", "b"])):
        names, hits = await count_plan_hits(author_names(minimum_rating).limit(limit))
        assert (names, hits) == (expected, 1)
    for _ in range(2):
        assert await author_names(0.0).first() == "a"
        assert await author_names(0.0).last() == "b"
        assert await author_names(2.0).last() == "a"


@pytest.mark.asyncio
async def test_count_and_exists_run_on_their_plan(db):
    await create_books()
    await Book.objects.filter(name="alpha").count()
    count, hits = await count_plan_hits(Book.objects.filter(name="nobody").count())
    assert (count, hits) == (0, 1)
    await Book.objects.filter(name="alpha").exists()
    exists, hits = await count_plan_hits(Book.objects.filter(name="beta").exists())
    assert (exists, hits) == (True, 1)
    exists, hits = await count_plan_hits(Book.objects.filter(name="nobody").exists())
    assert (exists, hits) == (False, 1)


@pytest.mark.asyncio
async def test_count_and_exists_with_a_keyset_boundary(db):
    """A keyset boundary isn't part of the count/exists key - such a query is built each time."""
    first, second = await create_books()
    for _ in range(2):
        assert await Book.objects.all().order_by("id").after_cursor(first.id).count() == 1
        assert await Book.objects.all().order_by("id").after_cursor(second.id).exists() is False
