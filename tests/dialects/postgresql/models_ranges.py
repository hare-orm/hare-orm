from hare import Model, fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.postgresql.fields.ranges import (
    DateRangeField,
    DateTimeRangeField,
    DecimalRangeField,
    IntRangeField,
)
from hare.fields.generated_field import GeneratedField


class RangeThing(Model):
    id = fields.IntField(primary_key=True)
    span = IntRangeField(null=True)


class DecimalRangeThing(Model):
    id = fields.IntField(primary_key=True)
    span = DecimalRangeField(null=True)


class DateRangeThing(Model):
    id = fields.IntField(primary_key=True)
    span = DateRangeField(null=True)


class DateTimeRangeThing(Model):
    id = fields.IntField(primary_key=True)
    span = DateTimeRangeField(null=True)


class GeneratedRangeThing(Model):
    id = fields.IntField(primary_key=True)
    low = fields.DecimalField(max_digits=10, decimal_places=2)
    high = fields.DecimalField(max_digits=10, decimal_places=2)
    span = GeneratedField(expression=RawSQLTerm("numrange(low, high)"), output_field=DecimalRangeField())
