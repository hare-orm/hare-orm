"""An UPDATE setting a PostgreSQL field to another field's value of a type its column doesn't take - an
interval and a number or a bigint of microseconds, arrays of other elements, ranges of other bounds - is
refused before any SQL, as the database would refuse it."""

import pytest

from hare import fields
from hare.dialects.postgresql.fields import IntervalField
from hare.dialects.postgresql.fields.ranges import BigIntRangeField, DateRangeField, IntRangeField
from hare.exceptions import FieldError
from hare.models.write.update.expression_assignment import ExpressionAssignment

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    ("field", "value_field", "message"),
    [
        (IntervalField(), fields.IntField(), "sets an interval and a number"),
        (fields.IntField(), IntervalField(), "sets a number and an interval"),
        (IntervalField(), fields.TimeDeltaField(), "sets an interval and a duration"),
        (fields.TimeDeltaField(), IntervalField(), "sets a duration and an interval"),
        (IntervalField(), fields.DateField(), "sets an interval and a date or a moment"),
        (
            fields.ArrayField(fields.IntField()),
            fields.ArrayField(fields.TextField()),
            "sets an array of a number and an array of text",
        ),
        (
            fields.ArrayField(fields.TextField()),
            fields.ArrayField(fields.IntField()),
            "sets an array of text and an array of a number",
        ),
        (
            fields.ArrayField(fields.ArrayField(fields.IntField())),
            fields.ArrayField(fields.IntField()),
            "sets an array of an array of a number and an array of a number",
        ),
        (IntRangeField(), DateRangeField(), r"sets a range \(int4range\) and a range \(daterange\)"),
        (IntRangeField(), BigIntRangeField(), r"sets a range \(int4range\) and a range \(int8range\)"),
    ],
)
def test_a_value_of_a_type_the_column_doesnt_take_is_refused(field, value_field, message):
    with pytest.raises(FieldError, match=message):
        ExpressionAssignment.raise_if_type_not_assignable("target", field, value_field)


@pytest.mark.parametrize(
    ("field", "value_field"),
    [
        (IntervalField(), IntervalField()),
        (fields.ArrayField(fields.IntField()), fields.ArrayField(fields.BigIntField())),
        (fields.ArrayField(fields.CharField(max_length=5)), fields.ArrayField(fields.TextField())),
        (IntRangeField(), IntRangeField()),
        (fields.TextField(), IntervalField()),
    ],
)
def test_a_value_of_a_type_the_column_takes_is_set(field, value_field):
    ExpressionAssignment.raise_if_type_not_assignable("target", field, value_field)
