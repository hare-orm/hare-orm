"""Summaries retain keyless row identity instead of reconstructing it through a PK."""

import pytest

from hare import Model, fields
from hare.exceptions import QueryError
from hare.query.functions import Coalesce, Count, Max, Sum
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", ["full", "only", "defer"])
async def test_keyless_distinct_exists_and_aggregate_use_selected_rows(contract_database, projection):
    class Book(Model):
        name = fields.TextField()
        score = fields.IntField()

        class Meta:
            primary_key = None

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for name, score in [("a", 1)] * 3 + [("b", 2)] * 2:
        await Book.objects.create(name=name, score=score)
    query = Book.objects
    if projection == "only":
        query = query.only("score")
    elif projection == "defer":
        query = query.defer("name")
    query = query.distinct().order_by("score")
    for offset in (0, 1, 2, 4):
        assert await query.offset(offset).exists() is (offset < 2)
    assert await query.aggregate(total=Sum("score"), number=Count("score")) == {"total": 3, "number": 2}
    with pytest.raises(QueryError, match="count.*distinct"):
        await query.count()
    assert await Book.objects.values_list("score", flat=True).distinct().count() == 2
    assert await Book.objects.aggregate(total=Sum("score"), number=Count("score")) == {"total": 7, "number": 5}


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", ["full", "only", "defer"])
async def test_keyless_aggregate_keeps_the_slice_and_its_multiplicity(contract_database, projection):
    class Reading(Model):
        sequence = fields.IntField()
        score = fields.IntField(null=True)

        class Meta:
            primary_key = None

    contract_database.register_live_models([Reading], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await Reading.objects.bulk_create([Reading(sequence=i, score=v) for i, v in enumerate([None, 4, 4, 7, None], 1)])
    rows = Reading.objects.filter(score__in=[None, 4]).order_by("sequence")
    if projection == "only":
        rows = rows.only("sequence")
    elif projection == "defer":
        rows = rows.defer("score")

    for offset, expected in (
        (0, {"rows": 3, "scores": 2}),
        (1, {"rows": 3, "scores": 2}),
        (4, {"rows": 0, "scores": 0}),
    ):
        assert await rows.offset(offset).limit(3).aggregate(rows=Count("sequence"), scores=Count("score")) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [True, False], ids=["keyed", "keyless"])
async def test_grouped_aggregate_takes_the_slice_after_grouping(contract_database, keyed):
    class Reading(Model):
        sequence = fields.IntField(primary_key=keyed, generated=False)
        band = fields.IntField()
        amount = fields.IntField()

        class Meta:
            if not keyed:
                primary_key = None

    contract_database.register_live_models([Reading], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    rows = [(1, 8), (1, 6), (1, 6), (2, 8), (2, 7), (3, 4)]
    await Reading.objects.bulk_create(
        [Reading(sequence=sequence, band=band, amount=amount) for sequence, (band, amount) in enumerate(rows, 1)]
    )
    for bias, expected in (
        (0, {"total": 19, "largest": 2, "bands": 2}),
        (15, {"total": 35, "largest": 3, "bands": 2}),
    ):
        grouped = (
            Reading.objects.group_by("band")
            .annotate(total=Sum("amount"), size=Count("amount"), ordering=Sum("amount") - Count("amount") * bias)
            .order_by("-ordering", "band")
            .offset(1)
        )
        metrics = {"total": Coalesce(Sum("total"), 0), "largest": Max("size"), "bands": Count("band")}
        assert await grouped.limit(2).aggregate(**metrics) == expected
        assert await grouped.limit(0).aggregate(**metrics) == {"total": 0, "largest": None, "bands": 0}


@pytest.mark.asyncio
async def test_keyless_annotation_summaries_preserve_implicit_groups(contract_database):
    class Book(Model):
        name = fields.TextField()
        score = fields.IntField()

        class Meta:
            primary_key = None

    contract_database.register_live_models([Book], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    for name, score in [("a", 1)] * 3 + [("b", 2)] * 2:
        await Book.objects.create(name=name, score=score)
    query = Book.objects.annotate(number=Count("score")).order_by("name")
    assert [(obj.name, obj.number) for obj in await query] == [("a", 3), ("b", 2)]
    for offset in (0, 1, 2, 3):
        assert await query.offset(offset).count() == max(0, 2 - offset)
        assert await query.offset(offset).exists() is (offset < 2)
    assert await query.aggregate(total=Sum("number")) == {"total": 5}
