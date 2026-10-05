import datetime
import uuid
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum

from hare import Model, fields
from hare.fields import Field
from hare.fields.validators import MaxLengthValidator


class PaintColor(StrEnum):
    RED = "red"
    BLUE = "blue"


class Priority(IntEnum):
    LOW = 1
    HIGH = 2


class Rank(Enum):
    FIRST = 1
    SECOND = 2


class PlainTextField(Field[str]):
    """A custom field keeping the base, unmodified to_db_value."""

    SQL_TYPE = "TEXT"
    field_type = str


class WritePathRecord(Model):
    id = fields.IntField(primary_key=True)
    number = fields.IntField(null=True)
    big_number = fields.BigIntField(null=True)
    small_number = fields.SmallIntField(null=True)
    data = fields.JSONField(null=True)
    secret_data = fields.JSONField(null=True, sensitive=True)
    secret_code = PlainTextField(null=True, sensitive=True, validators=[MaxLengthValidator(4)])
    secret_name = fields.CharField(max_length=4, null=True, sensitive=True)
    secret_number = fields.IntField(null=True, sensitive=True)
    duration = fields.TimeDeltaField(null=True)
    identifier = fields.UUIDField(null=True)
    rank = fields.CharEnumField(Rank, null=True)


class WritePathDefaults(Model):
    id = fields.IntField(primary_key=True)
    color = fields.CharEnumField(PaintColor, default="red")
    priority = fields.IntEnumField(Priority, default=2)
    price = fields.DecimalField(max_digits=6, decimal_places=2, default=1.5)
    price_text = fields.DecimalField(max_digits=6, decimal_places=2, default="2.5")
    exact_price = fields.DecimalField(max_digits=6, decimal_places=2, default=Decimal("3.5"))
    naive_moment = fields.DatetimeField(default=datetime.datetime(2020, 1, 1, 12, 0))
    text_moment = fields.DatetimeField(default="2020-01-01T12:00:00+03:00")
    current_moment = fields.DatetimeField(default=datetime.datetime.now)
    identifier = fields.UUIDField(default=lambda: str(uuid.UUID(int=5)))
    day = fields.DateField(default="2020-02-02")
    duration = fields.TimeDeltaField(default=5_000_000)
    ratio = fields.FloatField(default=Decimal("1.25"))
    count = fields.IntField(default="7")
    rank = fields.CharEnumField(Rank, default=lambda: 2)


class WritePathParent(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=20)


class WritePathChild(Model):
    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[WritePathParent] = fields.ForeignKeyField(
        "models.WritePathParent", related_name="children"
    )
