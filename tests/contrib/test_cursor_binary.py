"""Cursor pagination preserves typed scalar and container ordering values."""

import datetime
import os
from decimal import Decimal
from enum import Enum

import pytest

from hare import fields
from hare.contrib.request_query import CursorPagination, OrderingConfig, RequestQuery
from hare.contrib.request_query.enums import CursorDirection
from hare.contrib.request_query.paginations.cursor import Cursor
from hare.contrib.test import hare_test_context
from hare.models import Model


class BinaryEntry(Model):
    id = fields.IntField(primary_key=True)
    sort_key = fields.BinaryField()


class NestedCursorLabel(Enum):
    FIRST = "a"
    SECOND = "b"


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize(
    "values",
    [
        ("a", "b"),
        (1, 2),
        (False, True),
        ((1, 2), (2, 3)),
        (b"a", b"b"),
        (datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC), datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)),
        (datetime.timedelta(days=1), datetime.timedelta(days=2)),
        ([1], [2]),
        ({"a": 1}, {"b": 2}),
        (NestedCursorLabel.FIRST, NestedCursorLabel.SECOND),
        (None, "Z"),
        (Decimal("1.5"), Decimal("2.5")),
    ],
    ids=[
        "text",
        "integer",
        "boolean",
        "tuple",
        "bytes",
        "datetime",
        "interval",
        "list",
        "dict",
        "enum",
        "none",
        "decimal",
    ],
)
async def test_enum_pages_round_trip_every_members_python_value(values, composite):
    label_enum = Enum("CursorLabel", {"FIRST": values[0], "SECOND": values[1]})

    class EnumEntry(Model):
        id = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        if composite:
            pk = fields.CompositePrimaryKey("region", "id")
        label = fields.CharEnumField(label_enum)

    async with hare_test_context(
        [__name__], db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    ) as context:
        context.register_live_models([EnumEntry], "models", context.get_connection().connection_alias)
        await context.generate_schemas()

        class EnumEntries(RequestQuery[EnumEntry]):
            class Meta:
                queryset = EnumEntry.objects.all()
                ordering = OrderingConfig(default=("label",))
                pagination = CursorPagination(default_limit=1)

        await EnumEntry.objects.create(id=1, label=label_enum.FIRST)
        await EnumEntry.objects.create(id=2, label=label_enum.SECOND)
        first = await EnumEntries().page()
        second = await EnumEntries(cursor=first.next_cursor).page()
        back = await EnumEntries(cursor=second.previous_cursor).page()
        assert [[row.id for row in page.result] for page in (first, second, back)] == [[1], [2], [1]]
        assert second.result[0].label is label_enum.SECOND
        assert first.previous_cursor is None and second.next_cursor is None


@pytest.mark.database_independent
@pytest.mark.parametrize("shape", ["scalar", "array", "nested-array", "tuple", "named-tuple", "nested-tuple", "map"])
def test_field_based_cursors_share_the_container_conversion(shape):
    label_enum = Enum("ContainerCursorLabel", {"FIRST": (1, 2), "SECOND": (2, 3)})
    label_field = fields.CharEnumField(label_enum)
    field, value = label_field, label_enum.FIRST
    if shape == "array":
        field, value = fields.ArrayField(label_field), [label_enum.FIRST, None, label_enum.SECOND]
    elif shape == "nested-array":
        field, value = fields.ArrayField(fields.ArrayField(label_field)), [[label_enum.FIRST], [label_enum.SECOND]]
    elif shape == "tuple":
        field, value = fields.TupleField([label_field, fields.BinaryField()]), (label_enum.FIRST, b"\x00\x80\xff")
    elif shape in {"named-tuple", "nested-tuple"}:
        field = fields.TupleField({"label": label_field, "payload": fields.BinaryField()})
        value = {"label": label_enum.FIRST, "payload": b"\x00\x80\xff"}
        if shape == "nested-tuple":
            field, value = fields.ArrayField(field), [value]
    elif shape == "map":
        field = fields.MapField(label_field, fields.ArrayField(fields.CharEnumField(label_enum)))
        value = {label_enum.FIRST: [label_enum.SECOND]}
    cursor = Cursor(CursorDirection.NEXT, ("value",), (value,))
    assert Cursor.decode(cursor.encode((field,)), (field,), (False,)) == cursor


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("shape", ["array", "nested-array", "tuple", "named-tuple"])
async def test_enum_container_pages_forward_and_back(shape, composite):
    async with hare_test_context(
        [__name__], db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    ) as context:
        dialect_name = context.get_connection().dialect.name
        if dialect_name not in {"postgresql", "clickhouse"} or (
            shape in {"tuple", "named-tuple"} and dialect_name != "clickhouse"
        ):
            pytest.skip("this database has no column of the selected container type")
        label_enum = Enum("ContainerCursorLabel", {"FIRST": (1, 2), "SECOND": (2, 3)})
        value_field = fields.CharEnumField(label_enum)
        values = [label_enum.FIRST, label_enum.SECOND]
        if shape in {"array", "nested-array"}:
            value_field = fields.ArrayField(value_field)
            values = [[value] for value in values]
            if shape == "nested-array":
                value_field = fields.ArrayField(value_field)
                values = [[value] for value in values]
        elif shape == "tuple":
            value_field = fields.TupleField([value_field, fields.BinaryField()])
            values = [(value, b"\x80") for value in values]
        else:
            value_field = fields.TupleField({"label": value_field, "payload": fields.BinaryField()})
            values = [{"label": value, "payload": b"\x80"} for value in values]

        class ContainerEntry(Model):
            id = fields.IntField(primary_key=not composite, generated=False)
            region = fields.IntField(default=7)
            if composite:
                pk = fields.CompositePrimaryKey("region", "id")
            value = value_field

        context.register_live_models([ContainerEntry], "models", context.get_connection().connection_alias)
        await context.generate_schemas()

        class Entries(RequestQuery[ContainerEntry]):
            class Meta:
                queryset = ContainerEntry.objects.all()
                ordering = OrderingConfig(default=("value",))
                pagination = CursorPagination(default_limit=1)

        for number, value in enumerate(values, 1):
            await ContainerEntry.objects.create(id=number, value=value)
        first = await Entries().page()
        second = await Entries(cursor=first.next_cursor).page()
        back = await Entries(cursor=second.previous_cursor).page()
        assert [[row.id for row in page.result] for page in (first, second, back)] == [[1], [2], [1]]
        assert second.result[0].value == values[1]


@pytest.mark.database_independent
@pytest.mark.parametrize("value", [b"", b"ascii", b"\x00\x80\xff", None])
def test_a_binary_cursor_round_trip(value):
    cursor = Cursor(CursorDirection.NEXT, ("sort_key", "id"), (value, 3))
    assert Cursor.decode(cursor.encode(), (bytes, int), (True, False)) == cursor


@pytest.mark.asyncio
async def test_binary_pages_forward_and_back():
    async with hare_test_context([__name__], db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")):

        class BinaryEntries(RequestQuery[BinaryEntry]):
            class Meta:
                queryset = BinaryEntry.objects.all()
                ordering = OrderingConfig(default=("sort_key",))
                pagination = CursorPagination(default_limit=1)

        for row_id, value in enumerate((b"\x80", b"\x81", b"\xff"), 1):
            await BinaryEntry.objects.create(id=row_id, sort_key=value)
        assert await BinaryEntry.objects.order_by("sort_key").values_list("id", flat=True) == [1, 2, 3]
        first = await BinaryEntries().page()
        second = await BinaryEntries(cursor=first.next_cursor).page()
        third = await BinaryEntries(cursor=second.next_cursor).page()
        back = await BinaryEntries(cursor=third.previous_cursor).page()
        assert [[row.id for row in page.result] for page in (first, second, third, back)] == [[1], [2], [3], [2]]
        assert third.next_cursor is None
