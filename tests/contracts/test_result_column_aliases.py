"""SQL output names cannot replace source columns or lose their public result names."""

import pytest

from hare import Model, fields
from hare.contrib.test import requires_features
from hare.query.expressions import F, Lateral, OuterReference
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_annotations_keep_source_values_in_models_and_value_shapes(contract_database, composite):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False, source_field="wire_key")
        if composite:
            region = fields.IntField(default=7, source_field="wire_region")
            pk = fields.CompositePrimaryKey("region", "number")
        score = fields.IntField(source_field="wire_score")
        reserved = fields.IntField(source_field=f"hare result column {7 if composite else 6}", default=9)
        raw_score = fields.IntField(source_field="score", default=77)
        zero = fields.IntField(source_field="0", default=88)
        title = fields.TextField()

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for number in (1, 2):
        await Book.objects.create(number=number, score=number * 10, title=str(number))
    query = Book.objects.annotate(wire_score=F("score") + 100).order_by("number")
    for __ in range(2):
        for selected in (query, query.only("score", "raw_score", "reserved"), query.defer("title")):
            rows = await selected
            assert [(obj.score, obj.wire_score, obj.raw_score, obj.reserved) for obj in rows] == [
                (10, 110, 77, 9),
                (20, 120, 77, 9),
            ]
        assert [(obj.score, obj.wire_score) async for obj in query.iterator(chunk_size=1)] == [(10, 110), (20, 120)]
        assert await query.values("score", "wire_score", "raw_score") == [
            {"score": 10, "wire_score": 110, "raw_score": 77},
            {"score": 20, "wire_score": 120, "raw_score": 77},
        ]
        assert await query.values_list("score", "wire_score") == [(10, 110), (20, 120)]
        assert await query.values_list("wire_score", flat=True) == [110, 120]
        assert [obj.wire_score for obj in await query.order_by("-wire_score")] == [120, 110]
        assert await query.distinct().count() == 2
        assert await query.values("score", "wire_score").distinct().count() == 2
        assert sorted(
            (row["score"], row["wire_score"])
            for row in await query.values("score", "wire_score").union(query.values("score", "wire_score"))
        ) == [(10, 110), (20, 120)]
    long_name = "р" * 31 + "x"
    rows = await Book.objects.annotate(**{long_name: F("score") + 300}).order_by(long_name)
    assert [getattr(obj, long_name) for obj in rows] == [310, 320]
    assert await Book.objects.annotate(**{long_name: F("score") + 300}).order_by(long_name).values(long_name) == [
        {long_name: 310},
        {long_name: 320},
    ]


@pytest.mark.asyncio
async def test_related_partial_reading_retains_original_column_names(contract_database):
    class Author(Model):
        number = fields.IntField(primary_key=True, generated=False)
        score = fields.IntField(source_field="wire_rating")

    class Book(Model):
        number = fields.IntField(primary_key=True, generated=False)
        author = fields.ForeignKeyField(f"{CONTRACT_APP}.Author", db_constraint=False)
        score = fields.IntField(source_field="wire_score")
        raw_score = fields.IntField(source_field="score")
        shadow = fields.IntField(source_field="author.score")

    contract_database.register_live_models([Author, Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    author = await Author.objects.create(number=1, score=7)
    await Book.objects.create(number=1, author=author, score=10, raw_score=77, shadow=99)
    query = (
        Book.objects.select_related("author")
        .only("score", "raw_score", "shadow", "author__score")
        .annotate(wire_score=F("score") + 100)
    )
    for __ in range(2):
        row = await query.get()
        assert (row.score, row.raw_score, row.shadow, row.wire_score, row.author.score) == (10, 77, 99, 110, 7)


@requires_features(supports_lateral=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("shape", ["dict", "tuple"])
async def test_lateral_reads_actual_sql_names_of_public_columns(contract_database, composite, shape):
    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False, source_field="wire_key")
        if composite:
            region = fields.IntField(default=7)
            pk = fields.CompositePrimaryKey("region", "number")
        score = fields.IntField(source_field="wire_score")

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Book.objects.create(number=1, score=10)
    inner = Book.objects.filter(number=OuterReference("number")).annotate(wire_score=F("score") + 100)
    if composite:
        inner = inner.filter(region=OuterReference("region"))
    selected = inner.values("score", "wire_score") if shape == "dict" else inner.values_list("score", "wire_score")
    query = Book.objects.alias(selected=Lateral(selected)).annotate(
        plain_value=F("selected__score"), computed_value=F("selected__wire_score")
    )
    for __ in range(2):
        obj = await query.get()
        assert (obj.score, obj.plain_value, obj.computed_value) == (10, 10, 110)
