"""NativeEnumField: a PostgreSQL ENUM column - its type created by generate_schemas(), shared by two
fields, members written and read, filtered and ordered in the enum's order, labels refused - and the
names, enums and databases it refuses."""

from __future__ import annotations

from enum import Enum

import pytest

from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields.native_enum import NativeEnumField
from hare.exceptions import ConfigurationError, UnSupportedError, ValidationError
from tests.dialects.postgresql.models_native_enum import NativeEnumOrder, OrderStatus, Priority


@pytest.mark.asyncio
async def test_the_columns_are_of_their_enum_types(db_native_enum):
    rows = await db_native_enum.get_connection().execute_dicts(
        "SELECT column_name, udt_name FROM information_schema.columns WHERE table_name = 'native_enum_order' "
        "AND column_name <> 'id' ORDER BY column_name"
    )
    assert [(row["column_name"], row["udt_name"]) for row in rows] == [
        ("previous_status", "order_status"),
        ("priority", "order_priority"),
        ("status", "order_status"),
    ]
    labels = await db_native_enum.get_connection().execute_dicts(
        "SELECT enumlabel FROM pg_enum WHERE enumtypid = 'order_status'::regtype ORDER BY enumsortorder"
    )
    assert [row["enumlabel"] for row in labels] == ["new", "paid", "shipped"]


@pytest.mark.asyncio
async def test_members_are_written_read_filtered_and_ordered_in_the_enums_order(db_native_enum):
    await NativeEnumOrder.objects.create(id=1, status=OrderStatus.SHIPPED, priority=Priority.HIGH)
    await NativeEnumOrder.objects.create(id=2, status="new", previous_status=OrderStatus.PAID)
    await NativeEnumOrder.objects.create(id=3, status=OrderStatus.PAID)
    order = await NativeEnumOrder.objects.get(id=1)
    assert (order.status, order.previous_status, order.priority) == (OrderStatus.SHIPPED, None, Priority.HIGH)
    assert (await NativeEnumOrder.objects.get(id=2)).priority is Priority.LOW
    # The enum's order, not the labels' alphabetical one.
    assert await NativeEnumOrder.objects.order_by("status").values_list("id", flat=True) == [2, 3, 1]
    assert await NativeEnumOrder.objects.filter(status=OrderStatus.PAID).values_list("id", flat=True) == [3]
    assert await NativeEnumOrder.objects.filter(status__in=["new", OrderStatus.SHIPPED]).order_by("id").values_list(
        "id", flat=True
    ) == [1, 2]
    assert await NativeEnumOrder.objects.filter(status__gt=OrderStatus.NEW).count() == 2
    assert await NativeEnumOrder.objects.filter(previous_status__isnull=False).values_list("id", flat=True) == [2]
    assert await NativeEnumOrder.objects.filter(id=3).update(status=OrderStatus.SHIPPED) == 1
    assert await NativeEnumOrder.objects.filter(id=3).values_list("status", flat=True) == [OrderStatus.SHIPPED]
    assert await NativeEnumOrder.objects.filter(priority=Priority.HIGH).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_a_value_outside_the_enum_is_refused(db_native_enum):
    with pytest.raises(ValidationError):
        await NativeEnumOrder.objects.create(id=4, status="lost")
    with pytest.raises(ValidationError):
        await NativeEnumOrder.objects.filter(status="lost").count()


@pytest.mark.asyncio
async def test_generate_schemas_again_keeps_the_types(db_native_enum):
    await db_native_enum.get_connection().execute_script(
        db_native_enum.get_connection()
        .dialect.schema_editor_class(db_native_enum.get_connection())
        .enum_types.get_enum_type_create_sql(EnumType("order_status", ("new", "paid", "shipped")), safe=True)
    )


def test_the_type_name_and_the_labels():
    class ShippingMethod(Enum):
        AIR = "air"

    field = NativeEnumField(ShippingMethod)
    assert field.requires_enum_type == EnumType("shipping_method", ("air",))
    assert NativeEnumField(Priority, type_name="prio").requires_enum_type == EnumType("prio", ("1", "2"))


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: NativeEnumField(OrderStatus, type_name="Bad Name"), "takes a lowercase identifier"),
        (lambda: NativeEnumField(OrderStatus, type_name="x" * 64), "takes a lowercase identifier"),
        (lambda: NativeEnumField(Enum("Empty", {})), "has no member"),
        (lambda: NativeEnumField(Enum("Long", {"A": "x" * 64})), "at most 63 bytes"),
    ],
)
def test_a_wrong_type_is_refused(make, message):
    with pytest.raises(ConfigurationError, match=message):
        make()


def test_two_fields_naming_one_type_with_other_labels_are_refused():
    class Other(Enum):
        NEW = "new"

    first = NativeEnumField(OrderStatus)
    second = NativeEnumField(Other, type_name="order_status")
    models = [type("FakeModel", (), {"_meta": type("Meta", (), {"fields_map": {"a": first, "b": second}})()})]
    with pytest.raises(ConfigurationError, match="Two fields name the ENUM type 'order_status' with different labels"):
        BaseSchemaEditor.enum_types_class.get_required_enum_types(models)


def test_a_database_without_enum_types_refuses_the_field():
    sqlite_dialect = DialectRegistry.get_dialect("sqlite")
    with pytest.raises(UnSupportedError):
        NativeEnumField(OrderStatus).get_column_type(sqlite_dialect)
