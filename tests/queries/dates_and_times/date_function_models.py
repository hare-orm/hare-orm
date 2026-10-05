from hare import fields
from hare.models import Model


class Moment(Model):
    id = fields.IntField(primary_key=True)
    at = fields.DatetimeField()
    day = fields.DateField()
    clock = fields.TimeField()

    class Meta:
        table = "df_moment"
