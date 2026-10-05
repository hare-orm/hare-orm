from hare import fields
from hare.fields.db_defaults.now import Now
from hare.models import Model


class ZonedMoment(Model):
    id = fields.IntField(primary_key=True, generated=False)
    at = fields.DatetimeField()
    clock = fields.TimeField()
    stamped = fields.DatetimeField(db_default=Now())

    class Meta:
        table = "clickhouse_zoned_moment"
