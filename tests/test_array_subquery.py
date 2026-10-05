"""ArraySubquery: the one column a values() queryset selects, as an array per outer row, in the
queryset's order - PostgreSQL only, refused before the query is sent elsewhere."""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.dialects.postgresql.functions.array import ArraySubquery
from hare.exceptions import QueryError, UnSupportedError
from hare.query.expressions import OuterReference
from tests.models_prefetch_slices import Label, Novel, Writer
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(scope="module")
async def array_database():
    async with hare_test_context(
        modules=["tests.models_prefetch_slices"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture
async def writers(array_database):
    async with RollbackIsolation(array_database):
        anna = await Writer.objects.create(id=1, name="anna")
        await Writer.objects.create(id=2, name="boris")
        first_label = await Label.objects.create(id=1, name="a")
        second_label = await Label.objects.create(id=2, name="b")
        for novel_id, rating in ((1, 5), (2, 9), (3, 7)):
            novel = await Novel.objects.create(id=novel_id, title=f"n{novel_id}", rating=rating, writer=anna)
            await novel.labels.add(first_label, *([second_label] if novel_id != 2 else []))
        yield


def novels_of_writer(*fields: str):
    return Novel.objects.filter(writer=OuterReference("pk")).order_by("-rating").values(*fields)


@pytest.mark.asyncio
@pytest.mark.usefixtures("writers")
async def test_array_subquery_on_postgresql():
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        with pytest.raises(UnSupportedError, match="ArraySubquery"):
            await Writer.objects.annotate(titles=ArraySubquery(novels_of_writer("title")))
        return
    writers = await Writer.objects.order_by("id").annotate(
        titles=ArraySubquery(novels_of_writer("title")), ratings=ArraySubquery(novels_of_writer("rating"))
    )
    assert [(writer.titles, writer.ratings) for writer in writers] == [(["n2", "n3", "n1"], [9, 7, 5]), ([], [])]
    filtered = Writer.objects.annotate(titles=ArraySubquery(novels_of_writer("title")))
    assert await filtered.filter(titles__contains=["n1"]).values_list("name", flat=True) == ["anna"]
    assert await filtered.filter(titles__len=0).values_list("name", flat=True) == ["boris"]
    rows = await Novel.objects.order_by("id").values_list(
        "title",
        labels_of_novel=ArraySubquery(
            Label.objects.filter(novels=OuterReference("pk")).order_by("-name").values("name")
        ),
    )
    assert rows == [("n1", ["b", "a"]), ("n2", ["a"]), ("n3", ["b", "a"])]


@pytest.mark.asyncio
@pytest.mark.usefixtures("writers")
async def test_array_subquery_takes_one_column():
    with pytest.raises(QueryError, match="one column"):
        await Writer.objects.annotate(titles=ArraySubquery(novels_of_writer("title", "rating")))
    with pytest.raises(QueryError, match="one column"):
        await Writer.objects.annotate(titles=ArraySubquery(Novel.objects.filter(writer=OuterReference("pk"))))
