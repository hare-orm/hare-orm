import uuid

from hare import Model, fields
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions


class StrictOwner(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=40)

    class Meta:
        table = "strict_owner"
        table_options = [SqliteTableOptions(strict=True)]


class StrictRecord(Model):
    id = fields.BigIntField(primary_key=True)
    owner = fields.ForeignKeyField("models.StrictOwner", related_name="records", null=True)
    flag = fields.BooleanField(default=False)
    count = fields.SmallIntField(default=0)
    ratio = fields.FloatField(null=True)
    amount = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    label = fields.CharField(max_length=20, null=True)
    text = fields.TextField(null=True)
    day = fields.DateField(null=True)
    moment = fields.DatetimeField(null=True)
    clock = fields.TimeField(null=True)
    data = fields.JSONField(null=True)
    token = fields.UUIDField(default=uuid.uuid4)
    payload = fields.BinaryField(null=True)

    class Meta:
        table = "strict_record"
        table_options = [SqliteTableOptions(strict=True)]


class StrictKeyed(Model):
    code = fields.CharField(max_length=10, primary_key=True)
    hits = fields.IntField(default=0)

    class Meta:
        table = "strict_keyed"
        table_options = [SqliteTableOptions(strict=True, without_rowid=True)]
