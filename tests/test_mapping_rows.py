"""A client whose result rows are plain mappings (Features.supports_positional_rows off) - as a
third-party dialect's client may return them - runs every query through the by-name path."""

import pytest

from hare.core.caching.caches import Caches
from hare.dialects.base.results import StatementResult
from tests.testmodels import Tournament


@pytest.fixture
def mapping_rows_connection(db, monkeypatch):
    # A connection's features are fixed when it's created, so plans cached for this model on
    # the same connection name by earlier tests assume positional rows - dropped for this test.
    Caches.forget_model_caches([Tournament])
    connection = Tournament.get_connection()
    execute = connection.execute

    async def execute_query_as_mappings(query, values=None, **kwargs):
        result = await execute(query, values, **kwargs)
        return StatementResult(result.row_count, [dict(row) for row in result.rows], result.inserted_id)

    monkeypatch.setattr(connection, "execute", execute_query_as_mappings)
    monkeypatch.setattr(connection, "features", connection.features.replace(supports_positional_rows=False))
    yield connection
    Caches.forget_model_caches([Tournament])


@pytest.mark.asyncio
async def test_count_and_get_read_rows_by_name(mapping_rows_connection):
    first = await Tournament.objects.create(name="first")
    await Tournament.objects.create(name="second")

    assert await Tournament.objects.all().using(mapping_rows_connection).count() == 2
    assert await Tournament.objects.filter(name="first").using(mapping_rows_connection).count() == 1
    fetched = await Tournament.objects.all().using(mapping_rows_connection).get(pk=first.pk)
    assert fetched.name == "first"
    names = (
        await Tournament.objects.all().using(mapping_rows_connection).order_by("name").values_list("name", flat=True)
    )
    assert names == ["first", "second"]
