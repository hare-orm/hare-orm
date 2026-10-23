from __future__ import annotations

from hare import Model, fields
from hare.dialects.postgresql.fields import FixedCharField, IntervalField, NaiveDatetimeField, NaiveTimeField


class WallClockEntry(Model):
    id = fields.IntField(primary_key=True, generated=False)
    at = NaiveDatetimeField(null=True)
    opens = NaiveTimeField(null=True)
    lasts = IntervalField(null=True)
    code = FixedCharField(max_length=4, null=True)


class WallClockMoment(Model):
    """A wall clock beside a moment."""

    id = fields.IntField(primary_key=True, generated=False)
    at = NaiveDatetimeField(null=True)
    moment = fields.DatetimeField(null=True)
