import pytest

from hare.exceptions import FieldError
from hare.query.expressions import Exists, F, OuterReference, Subquery, Window
from hare.query.functions import Count, Lower
from hare.query.functions.window import RowNumber
from tests.testmodels import Author, Book


async def create_authors_with_books() -> tuple[Author, Author, Author]:
    top_rated_author = await Author.objects.create(name="A1")
    low_rated_author = await Author.objects.create(name="A2")
    author_without_books = await Author.objects.create(name="A3")
    await Book.objects.create(name="b1", author=top_rated_author, rating=5.0)
    await Book.objects.create(name="b2", author=top_rated_author, rating=2.0)
    await Book.objects.create(name="b3", author=low_rated_author, rating=1.0)
    return top_rated_author, low_rated_author, author_without_books


def best_rating_subquery() -> Subquery:
    return Subquery(Book.objects.filter(author_id=OuterReference("id")).order_by("-rating").limit(1).values("rating"))


def has_books_exists() -> Exists:
    return Exists(Book.objects.filter(author_id=OuterReference("id")))


@pytest.mark.asyncio
async def test_order_by_unselected_subquery_annotation_values_list(db):
    top_rated_author, low_rated_author, author_without_books = await create_authors_with_books()

    ascending = (
        await Author.objects.all().annotate(c=best_rating_subquery()).order_by("c", "id").values_list("id", flat=True)
    )
    descending = (
        await Author.objects.all().annotate(c=best_rating_subquery()).order_by("-c", "id").values_list("id", flat=True)
    )

    rated_ascending = [author_id for author_id in ascending if author_id != author_without_books.id]
    assert rated_ascending == [low_rated_author.id, top_rated_author.id]
    rated_descending = [author_id for author_id in descending if author_id != author_without_books.id]
    assert rated_descending == [top_rated_author.id, low_rated_author.id]


@pytest.mark.asyncio
async def test_order_by_unselected_exists_annotation_values_list(db):
    top_rated_author, low_rated_author, author_without_books = await create_authors_with_books()

    result = await Author.objects.all().annotate(c=has_books_exists()).order_by("c", "id").values_list("id", flat=True)

    assert result == [author_without_books.id, top_rated_author.id, low_rated_author.id]


@pytest.mark.asyncio
async def test_order_by_subquery_and_exists_alias_loading_models(db):
    top_rated_author, low_rated_author, author_without_books = await create_authors_with_books()

    by_exists = await Author.objects.all().alias(c=has_books_exists()).order_by("-c", "id")
    by_subquery = (
        await Author.objects.all().alias(c=best_rating_subquery()).order_by(F("c").desc(nulls_last=True), "id")
    )

    assert [author.id for author in by_exists] == [top_rated_author.id, low_rated_author.id, author_without_books.id]
    assert [author.id for author in by_subquery] == [
        top_rated_author.id,
        low_rated_author.id,
        author_without_books.id,
    ]


@pytest.mark.asyncio
async def test_first_and_last_by_subquery_alias(db):
    top_rated_author, low_rated_author, _author_without_books = await create_authors_with_books()
    queryset = Author.objects.filter(id__in=[top_rated_author.id, low_rated_author.id]).alias(c=best_rating_subquery())

    first_author = await queryset.order_by("c").first()
    last_author = await queryset.order_by("c").last()

    assert first_author is not None and first_author.id == low_rated_author.id
    assert last_author is not None and last_author.id == top_rated_author.id


@pytest.mark.asyncio
async def test_window_partition_and_order_by_subquery_annotation(db):
    top_rated_author, low_rated_author, author_without_books = await create_authors_with_books()

    result = (
        await Author.objects.all()
        .annotate(c=has_books_exists())
        .annotate(row_number=Window(RowNumber(), partition_by=["c"], order_by=["id"]))
        .order_by("id")
        .values_list("id", "row_number")
    )

    assert result == [(top_rated_author.id, 1), (low_rated_author.id, 2), (author_without_books.id, 1)]


@pytest.mark.asyncio
async def test_update_and_delete_ordered_by_subquery_alias_with_limit(db):
    await create_authors_with_books()
    author_name = Subquery(Author.objects.filter(id=OuterReference("author_id")).values("name"))

    updated_count = await Book.objects.all().alias(c=author_name).order_by("-c", "id").limit(1).update(name="updated")
    deleted_count = await Book.objects.all().alias(c=author_name).order_by("c", "id").limit(1).delete()

    assert updated_count == 1
    assert deleted_count == 1
    assert await Book.objects.filter(name="updated").values_list("name", flat=True) == ["updated"]
    assert sorted(await Book.objects.all().values_list("name", flat=True)) == ["b2", "updated"]


@pytest.mark.asyncio
async def test_after_cursor_by_subquery_alias_raises_field_error(db):
    await create_authors_with_books()

    with pytest.raises(FieldError, match="does not support ordering by an annotation"):
        await Author.objects.all().alias(c=best_rating_subquery()).order_by("c", "id").after_cursor(1.0, 1)


@pytest.mark.asyncio
async def test_group_by_subquery_annotation_values_list(db):
    await create_authors_with_books()

    result = (
        await Author.objects.all()
        .annotate(c=has_books_exists(), n=Count("id"))
        .group_by("c")
        .order_by("c")
        .values_list("c", "n")
    )

    assert result == [(False, 1), (True, 2)]


@pytest.mark.asyncio
async def test_group_by_plain_annotation_values_list(db):
    await Author.objects.create(name="Same")
    await Author.objects.create(name="SAME")
    await Author.objects.create(name="Other")

    result = (
        await Author.objects.all()
        .annotate(c=Lower("name"), n=Count("id"))
        .group_by("c")
        .order_by("c")
        .values_list("c", "n")
    )

    assert result == [("other", 1), ("same", 2)]


@pytest.mark.asyncio
async def test_group_by_unselected_exists_alias(db):
    await create_authors_with_books()

    result = (
        await Author.objects.all()
        .alias(c=has_books_exists())
        .annotate(n=Count("id"))
        .group_by("c")
        .order_by("n")
        .values_list("n", flat=True)
    )

    assert result == [1, 2]


@pytest.mark.asyncio
async def test_group_by_exists_annotation_values_and_count(db):
    await create_authors_with_books()

    values_result = (
        await Author.objects.all()
        .annotate(c=has_books_exists(), n=Count("id"))
        .group_by("c")
        .order_by("c")
        .values("c", "n")
    )
    instance_count = await Author.objects.all().annotate(c=has_books_exists()).group_by("c").count()

    assert values_result == [{"c": False, "n": 1}, {"c": True, "n": 2}]
    assert instance_count == 3


@pytest.mark.asyncio
async def test_group_by_exists_annotation_renamed_values(db):
    await create_authors_with_books()

    result = (
        await Author.objects.all()
        .annotate(c=has_books_exists(), n=Count("id"))
        .group_by("c")
        .order_by("c")
        .values(has_books="c", author_count="n")
    )

    assert result == [{"has_books": False, "author_count": 1}, {"has_books": True, "author_count": 2}]


@pytest.mark.asyncio
async def test_distinct_by_exists_alias_ordered(db):
    top_rated_author, _low_rated_author, author_without_books = await create_authors_with_books()

    result = (
        await Author.objects.all()
        .alias(c=has_books_exists())
        .order_by("c", "id")
        .distinct()
        .values_list("id", flat=True)
    )

    assert result[0] == author_without_books.id
    assert result[1] == top_rated_author.id


@pytest.mark.asyncio
async def test_distinct_on_ordered_by_exists_alias(db):
    top_rated_author, low_rated_author, author_without_books = await create_authors_with_books()
    await Author.objects.create(name="A1")

    result = (
        await Author.objects.all()
        .alias(c=has_books_exists())
        .distinct("name")
        .order_by("name", "-c", "id")
        .values_list("id", flat=True)
    )

    assert result == [top_rated_author.id, low_rated_author.id, author_without_books.id]


@pytest.mark.asyncio
async def test_update_and_delete_ordered_by_selected_annotation_with_limit(db):
    await create_authors_with_books()
    author_name = Subquery(Author.objects.filter(id=OuterReference("author_id")).values("name"))

    updated_count = (
        await Book.objects.all().annotate(c=author_name).order_by("-c", "id").limit(1).update(name="updated")
    )
    deleted_count = await Book.objects.all().annotate(c=Lower("name")).order_by("-c").limit(1).delete()

    assert updated_count == 1
    assert deleted_count == 1
    assert sorted(await Book.objects.all().values_list("name", flat=True)) == ["b1", "b2"]
