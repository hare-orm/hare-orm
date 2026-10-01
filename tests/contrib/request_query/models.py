from enum import IntEnum, StrEnum

from hare import fields
from hare.contrib.versioning import VersionedModel
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model
from hare.query.enums import LookupValueShape
from hare.query.filters import FieldLookup
from hare.query.filters.lookups import Lookups


class BookStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class TicketPriority(IntEnum):
    LOW = 1
    HIGH = 2


class ScoreField(fields.IntField):
    """An integer with lookups registered by the project."""

    @staticmethod
    def build_within(field: fields.IntField | None) -> FieldLookup:
        return FieldLookup(Lookups.between)


ScoreField.register_lookup("within", ScoreField.build_within, value_shape=LookupValueShape.RANGE, value_type=int)
ScoreField.register_lookup(
    "within_on_postgresql",
    ScoreField.build_within,
    value_shape=LookupValueShape.RANGE,
    value_type=int,
    dialects=("postgresql",),
)


class Author(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    rating = fields.IntField(null=True)
    score = ScoreField(default=0)


class Profile(Model):
    id = fields.IntField(primary_key=True)
    author = fields.OneToOneField("models.Author", related_name="profile")
    city = fields.CharField(max_length=50)


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
    status = fields.CharField(max_length=20, default=BookStatus.DRAFT)
    author = fields.ForeignKeyField("models.Author", related_name="books")
    tags = fields.ManyToManyField("models.Tag", related_name="books")
    published_at = fields.DatetimeField(null=True, description="When the book came out")


class OrderLine(Model):
    order_id = fields.IntField()
    line_no = fields.IntField()
    book = fields.ForeignKeyField("models.Book", related_name="lines")
    quantity = fields.IntField()

    pk = CompositePrimaryKey("order_id", "line_no")


class LineReturn(Model):
    id = fields.IntField(primary_key=True)
    line = fields.ForeignKeyField("models.OrderLine", related_name="returns")
    reason = fields.CharField(max_length=100)


class Refund(Model):
    id = fields.IntField(primary_key=True)
    line_return = fields.ForeignKeyField("models.LineReturn", related_name="refunds")
    amount = fields.IntField()


class Visit(Model):
    book = fields.ForeignKeyField("models.Book", related_name="visits")
    visitor = fields.CharField(max_length=50)

    class Meta:
        primary_key = None


class Note(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=100)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class NoteMark(Model):
    id = fields.IntField(primary_key=True)
    note = fields.ForeignKeyField("models.Note", related_name="marks")
    label = fields.CharField(max_length=50)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"


class Article(VersionedModel):
    title = fields.CharField(max_length=100)
    status = fields.CharField(max_length=20, default=BookStatus.DRAFT)


class Ticket(Model):
    id = fields.IntField(primary_key=True)
    company_id = fields.IntField()
    subject = fields.CharField(max_length=100)
    priority = fields.IntEnumField(TicketPriority, default=TicketPriority.LOW)

    class Meta:
        tenant_field = "company_id"
