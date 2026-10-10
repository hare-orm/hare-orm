"""Meta.versions = LatestVersions(): only the latest version of each record of a VersionedModel,
with one correlated subquery."""

import uuid

import pytest
import pytest_asyncio

from hare.contrib.request_query import LatestVersions, RequestQuery
from hare.contrib.test import assert_query_count
from hare.exceptions import ConfigurationError
from tests.contrib.request_query.models import Article, Book, BookStatus
from tests.contrib.request_query.queries import ArticleQuery, PublishedArticleQuery, VisibleArticleQuery

FIRST_RECORD = uuid.UUID(int=1)
SECOND_RECORD = uuid.UUID(int=2)


@pytest_asyncio.fixture
async def articles(db_request_query):
    """The first record in three versions - the last one a draft - and the second in one."""
    for record_id, version, title, status in (
        (FIRST_RECORD, 1, "Alpha one", BookStatus.PUBLISHED),
        (FIRST_RECORD, 2, "Alpha two", BookStatus.PUBLISHED),
        (FIRST_RECORD, 3, "Alpha three", BookStatus.DRAFT),
        (SECOND_RECORD, 1, "Beta one", BookStatus.PUBLISHED),
    ):
        await Article.objects.create(id=record_id, version=version, title=title, status=status)


@pytest.mark.asyncio
async def test_only_the_latest_version_of_each_record(articles):
    page = await ArticleQuery().page()
    assert [(article.title, article.version) for article in page.result] == [("Alpha three", 3), ("Beta one", 1)]
    assert page.count == 2
    assert await ArticleQuery().count() == 2


@pytest.mark.asyncio
async def test_one_query_with_a_correlated_subquery(articles):
    async with assert_query_count(1, using="models") as counter:
        await ArticleQuery().fetch()
    assert "EXISTS" in counter.queries[0].upper()


@pytest.mark.asyncio
async def test_a_filter_applies_to_the_latest_version_only(articles):
    assert await ArticleQuery(title="Alpha two").fetch() == []
    assert [article.title for article in await ArticleQuery(status=BookStatus.PUBLISHED).fetch()] == ["Beta one"]


@pytest.mark.asyncio
async def test_the_queryset_decides_the_versions_compared(articles):
    titles = [article.title for article in await PublishedArticleQuery().fetch()]
    assert titles == ["Alpha two", "Beta one"]


@pytest.mark.asyncio
async def test_a_version_the_request_may_not_see_hides_nothing(articles):
    titles = [article.title for article in await VisibleArticleQuery().fetch()]
    assert titles == ["Alpha two", "Beta one"]


def test_a_model_without_versions_is_refused(db_request_query):
    class LatestBookQuery(RequestQuery[Book]):
        class Meta:
            queryset = Book.objects.all()
            versions = LatestVersions()

    with pytest.raises(ConfigurationError, match="isn't a VersionedModel"):
        LatestBookQuery.get_declaration()
