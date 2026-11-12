"""Model row decoding preserves value errors, exact JSON and joined relation state."""

from dataclasses import replace

import pytest

from hare.contrib.test import capture_queries
from hare.query.rows.model_rows.model_rows import ModelRows
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from tests.shared_models.testmodels import DoubleFK, IntFields, JSONFields


@pytest.mark.asyncio
async def test_row_reader_error_propagates_without_retry(db, monkeypatch):
    await IntFields.objects.create(intnum=1)
    calls = []

    def fail_read(value):
        calls.append(value)
        raise TypeError("row cannot be read")

    get_columns = ModelRows.get_columns

    def failing_columns(self, connection, result):
        columns = get_columns(self, connection, result)
        return [
            replace(
                columns[0], entries=tuple((name, field, fail_read) for name, field, _reader in columns[0].entries)
            ),
            *columns[1:],
        ]

    monkeypatch.setattr(ModelRows, "get_columns", failing_columns)
    with pytest.raises(TypeError, match="row cannot be read"):
        await IntFields.objects.all()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_bulk_writer_does_not_replace_a_missing_value(db):
    obj = IntFields(intnum=1)
    object.__delattr__(obj, "intnum")
    types = IntFields.get_connection().dialect.types
    with pytest.raises(AttributeError):
        BulkWriteBatches.serialize_instances(IntFields, types, [obj], ["intnum"])


@pytest.mark.asyncio
async def test_model_rows_read_large_json_integers_exactly(db):
    long_integer = 123456789012345678901234567890
    await JSONFields.objects.create(data={"big": long_integer, "items": [long_integer]})
    (row,) = await JSONFields.objects.all()
    assert row.data == {"big": long_integer, "items": [long_integer]}
    assert type(row.data["big"]) is int


@pytest.mark.asyncio
async def test_nested_related_rows_keep_values_absence_and_connection(db):
    leaf = await DoubleFK.objects.create(name="leaf", left=None)
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)

    rows = {row.name: row for row in await DoubleFK.objects.all().select_related("left", "left__left")}
    assert rows["root"].left.name == "middle"
    assert rows["root"].left.left.name == "leaf"
    assert rows["root"].left.left.pk == leaf.pk
    assert rows["middle"].left.name == "leaf"
    assert rows["middle"].left.left is None
    assert rows["leaf"].left is None
    connection_alias = DoubleFK.get_connection().connection_alias
    assert rows["root"].left._connection_alias == connection_alias
    assert rows["root"].pk == root.pk and rows["middle"].pk == middle.pk


@pytest.mark.asyncio
async def test_nested_select_related_selects_each_relation_once(db):
    """Overlapping paths select each relation's columns once."""

    await DoubleFK.objects.create(name="leaf", left=None)
    async with capture_queries() as counter:
        await DoubleFK.objects.all().select_related("left", "left__left")
    (select_sql,) = [query for query in counter.queries if query.lstrip().upper().startswith("SELECT")]
    unquoted_sql = select_sql.replace('"', "").replace("`", "")
    # Once as the column, once as its label.
    assert unquoted_sql.count("doublefk__left.name") == 2
