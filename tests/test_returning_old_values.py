"""returning(old=...): a written row's fields as they were before the write, next to the written ones -
UPDATE and MERGE on PostgreSQL 18+; refused before any SQL elsewhere."""

from __future__ import annotations

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import FieldError, UnSupportedError
from hare.query.expressions import F
from tests.testmodels import IntFields, MergeStock


async def create_int_rows() -> None:
    for row_id in range(1, 4):
        await IntFields.objects.create(id=row_id, intnum=row_id * 10)


@requires_features(supports_returning_old_new=True)
@pytest.mark.asyncio
async def test_update_returns_the_old_values(db):
    await create_int_rows()
    rows = (
        await IntFields.objects.filter(id__gte=2)
        .update(intnum=F("intnum") + 5)
        .returning("id", "intnum", old=("intnum",))
    )
    assert sorted(rows, key=lambda row: row["id"]) == [
        {"id": 2, "intnum": 25, "old": {"intnum": 20}},
        {"id": 3, "intnum": 35, "old": {"intnum": 30}},
    ]
    for step in (1, 2):
        rows = (
            await IntFields.objects.filter(id=1).update(intnum=F("intnum") + step).returning("intnum", old=("intnum",))
        )
        assert rows[0]["intnum"] - rows[0]["old"]["intnum"] == step


@requires_features(supports_returning_old_new=True)
@pytest.mark.asyncio
async def test_merge_returns_the_old_values(db):
    await MergeStock.objects.create(sku="apple", count=3)
    rows = await (
        MergeStock.objects.merge([{"sku": "apple", "count": 2}, {"sku": "pear", "count": 7}], on="sku")
        .when_matched(update={"count": F("count") + F("merge_source__count")})
        .when_not_matched(insert={"sku": F("merge_source__sku"), "count": F("merge_source__count")})
        .returning("sku", "count", old=("count",))
    )
    assert sorted(rows, key=lambda row: row["sku"]) == [
        {"merge_action": "update", "sku": "apple", "count": 5, "old": {"count": 3}},
        {"merge_action": "insert", "sku": "pear", "count": 7, "old": {"count": None}},
    ]


@pytest.mark.asyncio
async def test_a_server_without_old_values_refuses_them(db, monkeypatch):
    await create_int_rows()
    client = Connections.get("models")
    monkeypatch.setattr(client, "features", client.features.replace(supports_returning_old_new=False))
    if not client.features.supports_returning:
        pytest.skip("The database has no RETURNING at all")
    with pytest.raises(UnSupportedError, match="old"):
        await IntFields.objects.filter(id=1).update(intnum=1).returning("intnum", old=("intnum",))
    assert await IntFields.objects.filter(id=1).values_list("intnum", flat=True) == [10]


def test_wrong_old_values_are_refused():
    query = IntFields.objects.filter(id=1).update(intnum=1)
    with pytest.raises(FieldError, match="name the fields returned as written too"):
        query.returning(old=("intnum",))
    with pytest.raises(FieldError, match="twice"):
        query.returning("id", old=("intnum", "intnum"))
    with pytest.raises(FieldError, match="takes the IntFields fields"):
        query.returning("id", old=("nothing",))
