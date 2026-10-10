from __future__ import annotations

from hare import Model, fields
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.postgresql.fields.ranges import DateRangeField
from hare.fields.composite_primary_key import CompositePrimaryKey


class OverlapRoomBooking(Model):
    id = fields.IntField(primary_key=True)
    room = fields.IntField()
    during = DateRangeField()

    class Meta:
        table = "overlap_room_booking"
        constraints = (
            UniqueConstraint(fields=("room", "during"), without_overlaps=True, name="room_booking_no_overlap"),
        )


class OverlapPricePeriod(Model):
    product = fields.CharField(max_length=20)
    valid = DateRangeField()
    price = fields.IntField()
    pk = CompositePrimaryKey("product", "valid", without_overlaps=True)

    class Meta:
        table = "overlap_price_period"
