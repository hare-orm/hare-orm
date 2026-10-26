import datetime
import uuid
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum

from hare import Model, fields
from hare.fields import Field
from hare.fields.validators import MaxLengthValidator
from tests.utils.database_under_test import DatabaseUnderTest


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
    name = fields.CharField(max_length=4, null=True)
    if DatabaseUnderTest.holds_decimal_fields():
        amount = fields.DecimalField(max_digits=6, decimal_places=2, null=True)


class WritePathSecretKey(Model):
    """A composite primary key with a sensitive part."""

    region = fields.CharField(max_length=20, sensitive=True)
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("region", "number")


class WritePathSecretTag(Model):
    """A sensitive primary key - a relation to it stores the secret in its own key column."""

    code = fields.CharField(max_length=20, primary_key=True, sensitive=True)


class WritePathTaggedNote(Model):
    id = fields.IntField(primary_key=True)
    tag = fields.ForeignKeyField("models.WritePathSecretTag", related_name="notes", null=True)


class WritePathDefaults(Model):
    id = fields.IntField(primary_key=True)
    color = fields.CharEnumField(PaintColor, default="red")
    priority = fields.IntEnumField(Priority, default=2)
    if DatabaseUnderTest.holds_decimal_fields():
        price = fields.DecimalField(max_digits=6, decimal_places=2, default=1.5)
    if DatabaseUnderTest.holds_decimal_fields():
        price_text = fields.DecimalField(max_digits=6, decimal_places=2, default="2.5")
    if DatabaseUnderTest.holds_decimal_fields():
        exact_price = fields.DecimalField(max_digits=6, decimal_places=2, default=Decimal("3.5"))
    naive_moment = fields.DatetimeField(default=datetime.datetime(2020, 1, 1, 12, 0))
    text_moment = fields.DatetimeField(default="2020-01-01T12:00:00+03:00")
    current_moment = fields.DatetimeField(default=datetime.datetime.now)
    identifier = fields.UUIDField(default=lambda: str(uuid.UUID(int=5)))
    day = fields.DateField(default="2020-02-02")
    duration = fields.TimeDeltaField(default=datetime.timedelta(seconds=5))
    ratio = fields.FloatField(default=Decimal("1.25"))
    count = fields.IntField(default=7)
    rank = fields.CharEnumField(Rank, default=lambda: 2)


class WritePathParent(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=20)


class WritePathChild(Model):
    id = fields.IntField(primary_key=True)
    parent: fields.ForeignKeyRelation[WritePathParent] = fields.ForeignKeyField(
        "models.WritePathParent", related_name="children"
    )


class WritePathTimes(Model):
    """A time of day and binary data."""

    id = fields.IntField(primary_key=True)
    clock = fields.TimeField(null=True)
    data = fields.BinaryField(null=True)
