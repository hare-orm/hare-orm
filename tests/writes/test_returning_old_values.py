"""returning(old=...): a written row's fields as they were before the write, next to the written ones -
UPDATE and MERGE on PostgreSQL 18+; refused before any SQL elsewhere."""

from __future__ import annotations

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import FieldError, UnSupportedError
from hare.query.expressions import F
from hare.query.hare_column_names import HareColumnNames
from hare.sql.constants import IDENTIFIER_LENGTH_LIMIT
from tests.shared_models.testmodels import IntFields, LongColumnNames, MergeStock


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


@pytest.mark.database_independent
def test_wrong_old_values_are_refused():
    query = IntFields.objects.filter(id=1).update(intnum=1)
    with pytest.raises(FieldError, match="name the fields returned as written too"):
        query.returning(old=("intnum",))
    with pytest.raises(FieldError, match="twice"):
        query.returning("id", old=("intnum", "intnum"))
    with pytest.raises(FieldError, match="Unknown field .nothing. in returning.. for model IntFields"):
        query.returning("id", old=("nothing",))


@pytest.mark.database_independent
def test_the_old_values_of_long_columns_come_under_names_within_the_limit():
    old_value_names = HareColumnNames.get_old_value_names(LongColumnNames)
    assert all(len(name.encode()) <= IDENTIFIER_LENGTH_LIMIT for name in old_value_names.values())
    assert len(set(old_value_names.values())) == len(old_value_names)


@requires_features(supports_returning_old_new=True)
@pytest.mark.asyncio
async def test_update_returns_the_old_values_of_columns_named_up_to_the_limit(db):
    """ "hare old " before a column of 61 bytes made 70 - PostgreSQL cut the names to 63, and the two old
    values came under one name the rows were then read by in full: KeyError."""
    await LongColumnNames.objects.create(a=1, b=2, first="f0", second="s0", third="t0")
    for step in (1, 2):
        rows = (
            await LongColumnNames.objects.filter(pk=(1, 2))
            .update(first=f"f{step}", second=f"s{step}", third=f"t{step}")
            .returning("pk", "first", "second", "third", old=("first", "second", "third"))
        )
        previous = step - 1
        assert rows == [
            {
                "pk": (1, 2),
                "first": f"f{step}",
                "second": f"s{step}",
                "third": f"t{step}",
                "old": {"first": f"f{previous}", "second": f"s{previous}", "third": f"t{previous}"},
            }
        ]
