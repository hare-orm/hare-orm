from hare import Model, fields
from hare.dialects.postgresql.fields.array import ArrayField
from hare.dialects.postgresql.fields.ranges import (
    DateRangeField,
    DateTimeRangeField,
    DecimalRangeField,
    IntRangeField,
)


class AggAuthor(Model):
    name = fields.CharField(max_length=255)


class AggBook(Model):
    title = fields.CharField(max_length=255)
    author: fields.ForeignKeyRelation[AggAuthor] = fields.ForeignKeyField("models.AggAuthor", related_name="books")
    rating = fields.IntField()
    published = fields.DatetimeField()
    in_print = fields.BooleanField(default=True)


class AggEntry(Model):
    author: fields.ForeignKeyRelation[AggAuthor] = fields.ForeignKeyField("models.AggAuthor", related_name="entries")
    label = fields.CharField(max_length=32, default="")
    payload = fields.JSONField[dict](null=True)
    duration = fields.TimeDeltaField(null=True)
    happened_at = fields.DatetimeField(null=True)
    price = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    release_date = fields.DateField(null=True)


class AggPeriod(Model):
    author: fields.ForeignKeyRelation[AggAuthor] = fields.ForeignKeyField("models.AggAuthor", related_name="periods")
    span = IntRangeField(null=True)
    decimal_span = DecimalRangeField(null=True)
    date_span = DateRangeField(null=True)
    moment_span = DateTimeRangeField(null=True)
    tags = ArrayField(base_field=fields.TextField(), null=True)
    matrix = ArrayField(base_field=ArrayField(base_field=fields.IntField()), null=True)
