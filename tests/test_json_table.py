"""JsonTable: the items of a JSON document as rows joined under a name - columns read, filtered and
ordered through <name>__<column>, a document without items kept, an ordinality column - on
PostgreSQL 17+; refused before any SQL elsewhere, and the arguments it refuses."""

from __future__ import annotations

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import FieldError, QueryError, UnSupportedError
from hare.query.expressions import F, JsonTable
from tests.testmodels import JSONFields

LINES_TABLE = JsonTable(
    "data",
    "$.lines[*]",
    {
        "sku": fields.CharField(max_length=20),
        "quantity": fields.IntField(),
        "unit": (fields.TextField(), "$.unit.name"),
    },
    ordinality="position",
)


async def create_orders() -> None:
    await JSONFields.objects.create(
        id=1,
        data={"lines": [{"sku": "tea", "quantity": 2, "unit": {"name": "box"}}, {"sku": "cup", "quantity": 12}]},
    )
    await JSONFields.objects.create(id=2, data={"lines": [{"sku": "pot", "quantity": 1}]})
    await JSONFields.objects.create(id=3, data={"lines": []})


@requires_features(supports_json_table=True)
@pytest.mark.asyncio
async def test_items_are_rows(db):
    await create_orders()
    rows = (
        await JSONFields.objects.alias(line=LINES_TABLE)
        .order_by("id", "line__position")
        .values_list("id", "line__position", "line__sku", "line__quantity", "line__unit")
    )
    assert rows == [
        (1, 1, "tea", 2, "box"),
        (1, 2, "cup", 12, None),
        (2, 1, "pot", 1, None),
        (3, None, None, None, None),
    ]


@requires_features(supports_json_table=True)
@pytest.mark.asyncio
async def test_columns_filter_and_compute(db):
    await create_orders()
    big = (
        await JSONFields.objects.alias(line=LINES_TABLE)
        .filter(line__quantity__gte=2)
        .order_by("id", "line__position")
        .values_list("id", "line__sku")
    )
    assert big == [(1, "tea"), (1, "cup")]
    doubled = (
        await JSONFields.objects.alias(line=LINES_TABLE)
        .filter(line__sku="pot")
        .annotate(twice=F("line__quantity") * 2)
        .values_list("twice", flat=True)
    )
    assert doubled == [2]
    assert await JSONFields.objects.alias(line=LINES_TABLE).filter(line__sku__isnull=True).values_list(
        "id", flat=True
    ) == [3]


@pytest.mark.asyncio
async def test_a_database_without_json_table_refuses_it(db, monkeypatch):
    client = Connections.get("models")
    monkeypatch.setattr(client, "features", client.features.replace(supports_json_table=False))
    with pytest.raises(UnSupportedError, match="JSON_TABLE"):
        await JSONFields.objects.alias(line=LINES_TABLE).values_list("line__sku")


@requires_features(supports_json_table=True)
@pytest.mark.asyncio
async def test_an_unknown_column_is_refused(db):
    with pytest.raises(FieldError, match="reads one of its columns"):
        await JSONFields.objects.alias(line=LINES_TABLE).values_list("line__price")


@pytest.mark.parametrize(
    ("arguments", "keyword_arguments", "message"),
    [
        (("data", "lines[*]", {"sku": fields.TextField()}), {}, "starting with \\$"),
        (("data", "$.lines[*]", {}), {}, "non-empty dict"),
        (("data", "$.lines[*]", {"a__b": fields.TextField()}), {}, "identifier"),
        (("data", "$.lines[*]", {"sku": "text"}), {}, "a field or a \\(field, path\\)"),
        (("data", "$.lines[*]", {"sku": (fields.TextField(), "sku")}), {}, "a field or a \\(field, path\\)"),
        (("data", "$.lines[*]", {"sku": fields.TextField()}), {"ordinality": "sku"}, "one of its columns"),
    ],
)
def test_wrong_arguments_are_refused(arguments, keyword_arguments, message):
    with pytest.raises(QueryError, match=message):
        JsonTable(*arguments, **keyword_arguments)
