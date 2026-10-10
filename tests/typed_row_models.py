from hare import fields
from hare.models import Model


class TypedRow(Model):
    """A nullable column of every scalar type, for functions writing each type the same way."""

    id = fields.IntField(primary_key=True)
    number = fields.IntField(null=True)
    big_number = fields.BigIntField(null=True)
    ratio = fields.FloatField(null=True)
    amount = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    name = fields.CharField(max_length=40, null=True)
    body = fields.TextField(null=True)
    flag = fields.BooleanField(null=True)
    day = fields.DateField(null=True)
    at = fields.DatetimeField(null=True)
    clock = fields.TimeField(null=True)
    span = fields.TimeDeltaField(null=True)
    uid = fields.UUIDField(null=True)
    payload = fields.BinaryField(null=True)
    data = fields.JSONField(null=True)

    class Meta:
        table = "typed_row"


class TypedRowNote(Model):
    """A note on a TypedRow, for paths into its JSON field through the relation."""

    id = fields.IntField(primary_key=True)
    row = fields.ForeignKeyField("models.TypedRow", related_name="notes")
