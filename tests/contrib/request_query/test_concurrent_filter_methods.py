"""The async filter methods of a request run at once, and their conditions keep the declared order."""

import asyncio

import pytest

from hare.contrib.request_query import RequestQuery
from hare.contrib.test import requires_features
from hare.query.expressions import Q
from hare.transactions.transactions import Transactions
from tests.contrib.request_query.models import Author, Book

#: How long a filter method waits for the other one - they only meet when both run at once.
MEETING_TIMEOUT_SECONDS = 5


class MeetingBookQuery(RequestQuery[Book]):
    """Two async filter methods that each wait for the other to start."""

    author_rating_at_least: int | None = None
    title_starts_with: str | None = None
    status_is_known: bool | None = None

    class Meta:
        queryset = Book.objects.all()

    def model_post_init(self, context: object) -> None:
        super().model_post_init(context)
        self._started: dict[str, asyncio.Event] = {"rating": asyncio.Event(), "title": asyncio.Event()}
        self._condition_order: list[str] = []

    async def meet(self, own: str, other: str) -> None:
        self._started[own].set()
        await asyncio.wait_for(self._started[other].wait(), MEETING_TIMEOUT_SECONDS)

    async def filter_author_rating_at_least(self, value: int) -> Q:
        await self.meet("rating", "title")
        author_ids = await Author.objects.filter(rating__gte=value).values_list("id", flat=True)
        return Q(author_id__in=list(author_ids))

    async def filter_title_starts_with(self, value: str) -> Q:
        await self.meet("title", "rating")
        book_ids = await Book.objects.filter(title__startswith=value).values_list("id", flat=True)
        return Q(id__in=list(book_ids))

    def filter_status_is_known(self, value: bool) -> Q | None:
        return Q(status__isnull=False) if value else None


class WrongResultBookQuery(RequestQuery[Book]):
    title_is: str | None = None

    class Meta:
        queryset = Book.objects.all()

    async def filter_title_is(self, value: str) -> dict[str, str]:
        return {"title": value}


async def titles(query: RequestQuery[Book]) -> list[str]:
    return sorted(book.title for book in await query.fetch())


@pytest.mark.asyncio
async def test_async_filter_methods_run_at_once(library):
    query = MeetingBookQuery(author_rating_at_least=4, title_starts_with="A", status_is_known=True)
    assert await titles(query) == ["Alpha"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_async_filter_methods_run_at_once_inside_a_transaction(library):
    async with Transactions.atomic():
        query = MeetingBookQuery(author_rating_at_least=4, title_starts_with="B")
        assert await titles(query) == ["Beta"]


@pytest.mark.asyncio
async def test_conditions_keep_the_declared_order(library):
    conditions = await MeetingBookQuery(
        author_rating_at_least=4, title_starts_with="A", status_is_known=True
    ).get_request_conditions()
    (joined,) = conditions
    assert [type(child) for child in joined.children] == [Q, Q, Q]
    assert "author_id__in" in joined.children[0].filters
    assert "id__in" in joined.children[1].filters
    assert joined.children[2].filters == {"status__isnull": False}


@pytest.mark.asyncio
async def test_an_async_filter_method_must_return_a_q(library):
    with pytest.raises(TypeError, match=r"filter_title_is\(\) must return a Q or None"):
        await WrongResultBookQuery(title_is="Alpha").fetch()
