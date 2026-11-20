"""Selected values from a named JOIN participate in distinct model-row identity."""

import pytest

from hare import Model, fields
from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import UnSupportedError
from hare.query.expressions import ArrayJoin, F, Lateral, OuterReference
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@requires_features(supports_array_join=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_array_join_annotation_keeps_distinct_rows_in_summaries_and_pages(contract_database, composite):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        tags = fields.ArrayField(fields.IntField())

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for number, tags in [(1, [1, 2]), (2, []), (3, [3, 3])]:
        await Book.objects.create(number=number, tags=tags)
    query = Book.objects.alias(tag=ArrayJoin("tags")).annotate(value=F("tag")).distinct().order_by("pk")
    expected = [(1, 1), (1, 2), (3, 3)]
    for __ in range(2):
        assert [(obj.number, obj.value) for obj in await query] == expected
        assert await query.count() == 3
        assert await query.values("number", "value").count() == 3
        for chunk_size in (1, 2):
            assert [(obj.number, obj.value) async for obj in query.iterator(chunk_size=chunk_size)] == expected
    assert await query.order_by("pk", "value").values_list("number", "value")[1:3] == expected[1:3]
    assert await Book.objects.order_by("pk").values_list("number", flat=True) == [1, 2, 3]


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_lateral_annotation_keeps_distinct_rows_in_summaries_and_pages(
    contract_database, monkeypatch, composite
):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")

    class Chapter(Model):
        id = fields.IntField(primary_key=True, generated=False)
        book_number = fields.IntField()
        book_region = fields.IntField(default=7)
        score = fields.IntField()

    contract_database.register_live_models([Book, Chapter], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for number in (1, 2, 3):
        await Book.objects.create(number=number)
    for chapter_id, (book_number, score) in enumerate([(1, 1), (1, 2), (3, 3), (3, 3)], start=1):
        await Chapter.objects.create(id=chapter_id, book_number=book_number, score=score)
    chapters = Chapter.objects.filter(book_number=OuterReference("number"))
    if composite:
        chapters = chapters.filter(book_region=OuterReference("region"))
    query = (
        Book.objects.alias(chapters=Lateral(chapters.values("score")))
        .annotate(value=F("chapters__score"))
        .distinct()
        .order_by("pk")
    )
    expected = [(1, 1), (1, 2), (2, None), (3, 3)]
    for __ in range(2):
        assert [(obj.number, obj.value) for obj in await query] == expected
        assert await query.count() == 4
        assert await query.values("number", "value").count() == 4
        for chunk_size in (1, 2):
            assert [(obj.number, obj.value) async for obj in query.iterator(chunk_size=chunk_size)] == expected
    assert await query.order_by("pk", "value").values_list("number", "value")[1:3] == expected[1:3]
    assert await Book.objects.order_by("pk").values_list("number", flat=True) == [1, 2, 3]
    connection = contract_database.get_connection()
    with monkeypatch.context() as patch:
        patch.setattr(connection, "features", connection.features.replace(supports_lateral=False))
        for build in (query.all, query.count, lambda: query.values_list("number", "value")):
            async with capture_queries(using=connection) as queries:
                with pytest.raises(UnSupportedError, match="needs a LATERAL subquery"):
                    await build()
            assert queries.count == 0
    assert await query.count() == 4
