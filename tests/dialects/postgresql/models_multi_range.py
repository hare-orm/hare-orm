from __future__ import annotations

from hare import Model, fields
from hare.dialects.postgresql.fields.multiranges import (
    BigIntMultiRangeField,
    DateMultiRangeField,
    DateTimeMultiRangeField,
    DecimalMultiRangeField,
    IntMultiRangeField,
)
from hare.dialects.postgresql.fields.ranges import DateRangeField


class RoomSchedule(Model):
    id = fields.IntField(primary_key=True)
    building = fields.CharField(max_length=20)
    busy_days = DateMultiRangeField(null=True)
    busy_times = DateTimeMultiRangeField(null=True)
    seats = IntMultiRangeField(null=True)
    serials = BigIntMultiRangeField(null=True)
    prices = DecimalMultiRangeField(null=True)

    class Meta:
        table = "multi_range_room_schedule"


class RoomBooking(Model):
    id = fields.IntField(primary_key=True)
    building = fields.CharField(max_length=20)
    during = DateRangeField()

    class Meta:
        table = "multi_range_room_booking"
