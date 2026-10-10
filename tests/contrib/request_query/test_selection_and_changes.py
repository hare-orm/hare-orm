"""The fields and relations a request loads, word search, changing rows, and building a query from a
query string."""

import pytest

from hare.contrib.request_query import (
    FieldsConfig,
    IncludeConfig,
    InvalidRequestQuery,
    RequestQuery,
)
from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError, DoesNotExist, QueryError
from hare.transactions.transactions import Transactions
from tests.contrib.request_query.models import Author, Book, BookStatus, OrderLine
from tests.contrib.request_query.queries import BookQuery, CatalogBookQuery, OrderLineQuery, PublishedBookQuery


@pytest.mark.asyncio
async def test_only_the_fields_a_request_names_are_loaded(library):
    books = await CatalogBookQuery(fields="title", author=1).fetch()
    assert [book.title for book in books] == ["Alpha", "Beta"]
    assert books[0].pk == 1
    with pytest.raises(AttributeError):
        books[0].status  # noqa: B018 - reading a field .only() didn't load
    page = await CatalogBookQuery(fields="status,author_id").page()
    assert [book.status for book in page.result] == [
        BookStatus.PUBLISHED,
        BookStatus.DRAFT,
        BookStatus.PUBLISHED,
        BookStatus.DRAFT,
        BookStatus.PUBLISHED,
    ]


@pytest.mark.asyncio
async def test_relations_a_request_names_are_loaded(library):
    books = await CatalogBookQuery(include="author,tags", author=1).fetch()
    assert [book.author.name for book in books] == ["Anna", "Anna"]
    assert [sorted(tag.name for tag in book.tags) for book in books] == [["fantasy"], ["history"]]
    book = await CatalogBookQuery(include="author__profile", author=1, search="alpha").get()
    assert book.author.profile.city == "Moscow"


@pytest.mark.asyncio
async def test_unknown_fields_or_relations_are_refused(db_request_query):
    with pytest.raises(InvalidRequestQuery) as error:
        CatalogBookQuery(fields="title,secret")
    assert error.value.errors[0]["loc"] == ["fields"]
    with pytest.raises(InvalidRequestQuery) as error:
        CatalogBookQuery(include="lines")
    assert error.value.errors[0]["loc"] == ["include"]


@pytest.mark.asyncio
async def test_fields_and_include_options_are_checked(library):
    class RelationAsFieldQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            fields = FieldsConfig(fields=("title", "author"))

    class FieldAsRelationQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            include = IncludeConfig(relations=("author__name",))

    with pytest.raises(ConfigurationError, match="isn't a field of Book itself"):
        RelationAsFieldQuery.get_declaration()
    with pytest.raises(ConfigurationError, match="isn't a relation of Book"):
        FieldAsRelationQuery.get_declaration()
    with pytest.raises(ConfigurationError, match="non-empty tuple"):
        FieldsConfig(fields=())
    with pytest.raises(ConfigurationError, match="each name once"):
        IncludeConfig(relations=("author", "author"))


@pytest.mark.asyncio
async def test_search_word_by_word(library):
    assert [book.title for book in await CatalogBookQuery(search="anna beta").fetch()] == ["Beta"]
    assert [book.title for book in await CatalogBookQuery(search="  boris   gam ").fetch()] == ["Gamma"]
    assert await CatalogBookQuery(search="anna delta").fetch() == []
    # Without splitting, the whole text is one value.
    assert await BookQuery(search="anna beta").fetch() == []


@pytest.mark.asyncio
async def test_update_the_filtered_rows(library):
    assert await BookQuery(author=1).update(status=BookStatus.DRAFT) == 2
    assert sorted(await Book.objects.filter(status=BookStatus.DRAFT).values_list("title", flat=True)) == [
        "Alpha",
        "Beta",
        "Epsilon",
    ]
    assert await BookQuery(tag_ids=[1, 2]).update(title="Tagged") == 4
    assert await Book.objects.filter(title="Tagged").count() == 4
    assert await OrderLineQuery(pk__in=["1,1", "2,2"]).update(quantity=9) == 2
    assert sorted(await OrderLine.objects.filter(quantity=9).values_list("order_id", "line_no")) == [(1, 1), (2, 2)]


@pytest.mark.asyncio
async def test_update_needs_a_filter_and_values_and_keeps_to_the_access_condition(library):
    with pytest.raises(QueryError, match="needs a filter"):
        await BookQuery().update(status=BookStatus.DRAFT)
    with pytest.raises(QueryError, match="needs the values"):
        await BookQuery(author=1).update()
    assert await PublishedBookQuery(author=1).update(title="Changed") == 1
    assert await Book.objects.get(title="Changed") == library.books["Alpha"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_get_for_update(library):
    async with Transactions.atomic("models"):
        book = await BookQuery(id=3).get_for_update()
        book.title = "Gamma II"
        await book.save()
    assert (await Book.objects.get(id=3)).title == "Gamma II"
    async with Transactions.atomic("models"):
        assert (await BookQuery(tag_ids=[2], author=2).get_for_update()).id == 3
        with pytest.raises(DoesNotExist):
            await BookQuery(id=99).get_for_update()


@pytest.mark.asyncio
async def test_from_a_query_string(library):
    query = BookQuery.from_query_string("author_ids=1&author_ids=2&status=published&ordering=-title&unknown=1")
    assert query.author_ids == [1, 2] and query.status is BookStatus.PUBLISHED
    assert [book.title for book in await query.fetch()] == ["Gamma", "Delta", "Alpha"]
    lines = OrderLineQuery.from_query_string("pk__in=1,1&pk__in=3,2")
    assert sorted(line.pk for line in await lines.fetch()) == [(1, 1), (3, 2)]
    assert OrderLineQuery.from_query_string("pk=2,1").pk == (2, 1)
    assert BookQuery.from_query_string("ids=1,2&ids=5").ids == [1, 2, 5]
    assert BookQuery.from_query_string("author=1", author=2).author == 2
    with pytest.raises(InvalidRequestQuery):
        BookQuery.from_query_string("author=anna")


@pytest.mark.asyncio
async def test_from_query_parameters_keeps_the_last_of_a_single_value(library):
    query = BookQuery.from_query_parameters([("author", "1"), ("author", "2")])
    assert query.author == 2
    assert (await Author.objects.get(id=2)).name == "Boris"
