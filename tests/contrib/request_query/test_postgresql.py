"""A PostgreSQL request query may use what only PostgreSQL runs; the base request query may not."""

import pytest

from hare.contrib.request_query import RequestQuery, SearchConfig
from hare.contrib.test import requires_features
from hare.dialects.postgresql.request_query import PostgresqlRequestQuery
from hare.dialects.sqlite.request_query import SqliteRequestQuery
from hare.exceptions import ConfigurationError
from tests.contrib.request_query.models import Author, Book


class TrigramBookQuery(PostgresqlRequestQuery[Book]):
    title__trigram_similar: str | None = None
    author__score__within_on_postgresql: tuple[int, int] | None = None

    class Meta:
        queryset = Book.objects.all()
        search = SearchConfig(fields=("title", "author__name"), lookup="trigram_similar")
        pagination = None


class FullTextBookQuery(PostgresqlRequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all()
        search = SearchConfig(fields=("title",), lookup="search")
        pagination = None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgresql_lookups_in_a_postgresql_request_query(db_request_query, library):
    await db_request_query.get_connection().execute_script("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    titles = sorted(book.title for book in await TrigramBookQuery(title__trigram_similar="Alphaa").fetch())
    assert titles == ["Alpha"]
    by_score = await TrigramBookQuery(author__score__within_on_postgresql=(40, 60)).fetch()
    assert sorted(book.title for book in by_score) == ["Delta", "Gamma"]
    assert sorted(book.title for book in await FullTextBookQuery(search="gamma").fetch()) == ["Gamma"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_the_base_and_the_sqlite_request_query_refuse_postgresql_lookups(library):
    class BaseTrigramQuery(RequestQuery[Book]):
        title__trigram_similar: str | None = None

        class Meta:
            queryset = Book.objects.all()

    class SqliteAuthorQuery(SqliteRequestQuery[Author]):
        name: str | None = None

        class Meta:
            queryset = Author.objects.all()

    with pytest.raises(ConfigurationError, match="doesn't run on sqlite"):
        BaseTrigramQuery.get_declaration()
    with pytest.raises(ConfigurationError, match="is a sqlite request query"):
        SqliteAuthorQuery.get_declaration()
