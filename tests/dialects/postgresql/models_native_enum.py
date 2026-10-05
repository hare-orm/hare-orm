from __future__ import annotations

from enum import Enum, IntEnum

from hare import Model, fields
from hare.dialects.postgresql.fields.native_enum import NativeEnumField


class OrderStatus(Enum):
    NEW = "new"
    PAID = "paid"
    SHIPPED = "shipped"


class Priority(IntEnum):
    LOW = 1
    HIGH = 2


class NativeEnumOrder(Model):
    """Two fields of one ENUM type, and an int enum's type of its own name."""

    id = fields.IntField(primary_key=True)
    status = NativeEnumField(OrderStatus)
    previous_status = NativeEnumField(OrderStatus, null=True)
    priority = NativeEnumField(Priority, type_name="order_priority", default=Priority.LOW)

    class Meta:
        table = "native_enum_order"
