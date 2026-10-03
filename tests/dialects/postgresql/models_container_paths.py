from hare import Model, fields
from hare.dialects.postgresql.fields.array import ArrayField
from hare.dialects.postgresql.fields.ranges import (
    DateRangeField,
    DateTimeRangeField,
    DecimalRangeField,
    IntRangeField,
)


class Shelf(Model):
    id = fields.IntField(primary_key=True)


class Crate(Model):
    id = fields.IntField(primary_key=True)
    shelf = fields.ForeignKeyField("models.Shelf", related_name="crates", null=True)
    span = IntRangeField(null=True)
    days = DateRangeField(null=True)
    moments = DateTimeRangeField(null=True)
    prices = DecimalRangeField(null=True)
    numbers = ArrayField(fields.IntField(), null=True)
    words = ArrayField(fields.CharField(max_length=20), null=True)
    grid = ArrayField(ArrayField(fields.IntField()), null=True)
