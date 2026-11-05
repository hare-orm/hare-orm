"""A request query's rows filled from other services (``Meta.api_getter``) - by ``fetch()``, the three
paginations and ``get()``, for the targets a request's ``fields`` names, and by a handler after a create."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx
import pytest

from hare.contrib.frameworks.error_answers import ErrorAnswers
from hare.contrib.request_query import (
    ApiGetterTrigger,
    ApiGetterTriggers,
    ApiRequestError,
    ApiSource,
    ApiValueMapping,
    CursorPagination,
    FieldsConfig,
    IncludeConfig,
    OffsetPagination,
    OrderingConfig,
    RequestQuery,
    ScrollPagination,
)
from hare.contrib.request_query.api_getter.api_http_requests import ApiHttpRequests
from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError
from hare.transactions.transactions import Transactions
from tests.contrib.request_query.models import Book

PEOPLE = {1: "Anna Petrova", 2: "Boris Ivanov", 3: "Clara Smirnova"}

PEOPLE_SOURCE = ApiSource(
    url="https://people.test/people",
    key="author_id",
    mappings=(ApiValueMapping(set_key="author_card"),),
    name="people",
    query_parameter="ids",
    match_key="id",
    items_path="result",
)


class BookCardQuery(RequestQuery[Book]):
    author: int | None = None

    class Meta:
        queryset = Book.objects.all()
        ordering = OrderingConfig(fields=("title",), default=("title",))
        fields = FieldsConfig(fields=("title", "status", "author_card", "heading"))
        include = IncludeConfig(relations=("author",))
        pagination = OffsetPagination(default_limit=2)
        api_getter = (PEOPLE_SOURCE, ApiValueMapping(set_key="heading", key="title"))


class BookCardScrollQuery(BookCardQuery):
    class Meta:
        pagination = ScrollPagination(default_limit=2)


class BookCardCursorQuery(BookCardQuery):
    class Meta:
        pagination = CursorPagination(default_limit=2)


class BareBookCardQuery(BookCardQuery):
    """A query of another service asking for the bare rows."""

    async def loads_api_values(self) -> bool:
        return False


class CreatedBookQuery(RequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all()
        api_getter = (
            ApiSource(
                url="https://people.test/people",
                key="author_id",
                mappings=(ApiValueMapping(set_key="author_card"),),
                query_parameter="ids",
                match_key="id",
                items_path="result",
                triggers=ApiGetterTriggers(on_read=False, on_create=True),
            ),
        )


@pytest.fixture
def people_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    sent_requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request)
        ids = [int(person_id) for person_id in request.url.params.get_list("ids")]
        return httpx.Response(
            200,
            json={
                "result": [{"id": person_id, "name": PEOPLE[person_id]} for person_id in ids if person_id in PEOPLE]
            },
        )

    monkeypatch.setattr(ApiHttpRequests, "transport", httpx.MockTransport(answer))
    return sent_requests


def get_card_names(books: list[Any]) -> list[str | None]:
    return [book.author_card and book.author_card["name"] for book in books]


@pytest.mark.asyncio
async def test_fetch_and_get_fill_the_rows(library, people_requests: list[httpx.Request]) -> None:
    books = await BookCardQuery().fetch()
    assert [book.title for book in books] == ["Alpha", "Beta", "Delta", "Epsilon", "Gamma"]
    assert get_card_names(books) == [PEOPLE[1], PEOPLE[1], PEOPLE[2], PEOPLE[3], PEOPLE[2]]
    assert [book.heading for book in books] == [book.title for book in books]
    assert len(people_requests) == 1
    assert sorted(people_requests[0].url.params.get_list("ids")) == ["1", "2", "3"]
    book = await BookCardQuery(author=3).get()
    assert book.author_card == {"id": 3, "name": PEOPLE[3]}


@pytest.mark.asyncio
@pytest.mark.parametrize("query_class", [BookCardQuery, BookCardScrollQuery, BookCardCursorQuery])
async def test_each_pagination_fills_the_rows_of_its_page(
    library, people_requests: list[httpx.Request], query_class: type[BookCardQuery]
) -> None:
    page = await query_class().page()
    assert get_card_names(page.result) == [PEOPLE[1], PEOPLE[1]]
    assert [request.url.params.get_list("ids") for request in people_requests] == [["1"]]


@pytest.mark.asyncio
async def test_only_the_targets_a_request_names_are_asked_for(library, people_requests: list[httpx.Request]) -> None:
    books = await BookCardQuery(fields="title", author=1).fetch()
    assert people_requests == []
    assert not hasattr(books[0], "author_card")
    books = await BookCardQuery(fields="author_card", author=1).fetch()
    assert get_card_names(books) == [PEOPLE[1], PEOPLE[1]]
    with pytest.raises(AttributeError):
        books[0].status  # noqa: B018 - a field .only() didn't load
    books = await BookCardQuery(fields="heading", author=2).fetch()
    assert [book.heading for book in books] == ["Delta", "Gamma"]
    assert len(people_requests) == 1


@pytest.mark.asyncio
async def test_a_query_can_leave_its_rows_bare(library, people_requests: list[httpx.Request]) -> None:
    books = await BareBookCardQuery(author=1).fetch()
    assert people_requests == []
    assert not hasattr(books[0], "author_card")


@pytest.mark.asyncio
async def test_a_handler_fills_the_rows_it_created(library, people_requests: list[httpx.Request]) -> None:
    query = CreatedBookQuery()
    assert not hasattr((await query.fetch())[0], "author_card")
    book = await Book.objects.create(id=99, title="Omega", author_id=2)
    (book,) = await query.load_api_values([book], ApiGetterTrigger.CREATE)
    assert book.author_card["name"] == PEOPLE[2]
    assert len(people_requests) == 1


@requires_features(supports_transactions=True, supports_select_for_update=True)
@pytest.mark.asyncio
async def test_a_row_for_update_is_left_bare(library, people_requests: list[httpx.Request]) -> None:
    async with Transactions.atomic("models"):
        book = await BookCardQuery(author=3).get_for_update()
    assert not hasattr(book, "author_card")
    assert people_requests == []


@pytest.mark.asyncio
async def test_a_required_source_failing_is_a_bad_gateway(library, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ApiHttpRequests, "transport", httpx.MockTransport(lambda request: httpx.Response(503)))

    class RequiredQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            api_getter = (
                ApiSource(
                    url="https://people.test/people",
                    key="author_id",
                    mappings=(ApiValueMapping(set_key="author_card"),),
                    query_parameter="ids",
                    required=True,
                ),
            )

    with pytest.raises(ApiRequestError) as error:
        await RequiredQuery().fetch()
    assert ErrorAnswers.get_status(error.value) is HTTPStatus.BAD_GATEWAY


@pytest.mark.asyncio
async def test_a_field_naming_no_target_is_refused(db_request_query) -> None:
    class UnknownFieldQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            fields = FieldsConfig(fields=("author_card",))

    with pytest.raises(ConfigurationError, match="author_card"):
        UnknownFieldQuery.get_declaration()


class AuthorCountryQuery(RequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all()
        include = IncludeConfig(relations=("author", "tags"))
        pagination = None
        api_getter = (
            ApiSource(
                url="https://people.test/people",
                key="author.id",
                mappings=(ApiValueMapping(set_key="author.card", key="name"),),
                query_parameter="ids",
                match_key="id",
                items_path="result",
            ),
            ApiSource(
                url="https://people.test/people",
                key="tags.id",
                mappings=(ApiValueMapping(set_key="tags.card", key="name"),),
                query_parameter="ids",
                match_key="id",
                items_path="result",
            ),
        )


@pytest.mark.asyncio
async def test_a_relation_the_rows_didn_t_load_holds_no_value(library, people_requests: list[httpx.Request]) -> None:
    """A path through a relation not loaded met its lazy query and raised AttributeError setting a target on it."""
    books = await AuthorCountryQuery().where(id=1).fetch()
    assert people_requests == []
    books = await AuthorCountryQuery(include="author,tags").where(id=3).fetch()
    assert books[0].author.card == PEOPLE[2]
    assert [tag.card for tag in books[0].tags] == [PEOPLE[1], PEOPLE[2]]
