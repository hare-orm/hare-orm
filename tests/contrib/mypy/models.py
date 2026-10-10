"""The models the mypy plugin tests type-check queries of - every type of field, relation and key the
plugin reads, plus a custom field, lookup and transform."""

from enum import StrEnum
from typing import Self, TypedDict

from hare import fields
from hare.fields import CASCADE, SET_NULL
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model, swappable
from hare.query.enums import LookupValueShape
from hare.query.filters import FieldLookup
from hare.query.filters.lookups.lookups import Lookups
from hare.query.managers.manager import Manager
from hare.query.queryset import QuerySet


class ShelfStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Address(TypedDict):
    city: str
    street: str


class RatingField(fields.IntField):
    """An integer with a lookup registered by the project."""

    @staticmethod
    def build_between(field: fields.IntField | None) -> FieldLookup:
        return FieldLookup(Lookups.between)


RatingField.register_lookup(
    "rated_between", RatingField.build_between, value_shape=LookupValueShape.RANGE, value_type=int
)


class Writer(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    nickname = fields.CharField(max_length=50, null=True)
    born = fields.DateField(null=True)
    rating = RatingField(default=0)
    address = fields.JSONField[Address](null=True)

    volumes: fields.ReverseRelation["Volume"]

    class Meta:
        table = "typing_writer"


class Shelf(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=20)
    status = fields.CharEnumField(ShelfStatus, default=ShelfStatus.OPEN)

    class Meta:
        table = "typing_shelf"


class Label(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "typing_label"


class VolumeQuerySet(QuerySet["Volume"]):
    """A queryset class of the project's own, not passing the annotations type parameter on."""

    def priced(self) -> Self:
        return self.filter(price__gt=0)


class Volume(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
    price = fields.DecimalField(max_digits=8, decimal_places=2)
    published = fields.DatetimeField()
    writer = fields.ForeignKeyField(Writer, related_name="volumes", on_delete=CASCADE)
    shelf = fields.ForeignKeyField(Shelf, related_name="volumes", on_delete=SET_NULL, null=True)
    labels = fields.ManyToManyField(Label, related_name="volumes")

    selection = Manager(VolumeQuerySet)

    class Meta:
        table = "typing_volume"


class WriterCard(Model):
    writer = fields.OneToOneField(Writer, related_name="card", on_delete=CASCADE, primary_key=True)
    issued = fields.DateField()

    class Meta:
        table = "typing_writer_card"


class Printing(Model):
    pk = CompositePrimaryKey("edition", "number")
    edition = fields.IntField()
    number = fields.IntField()
    copies = fields.IntField(default=0)

    class Meta:
        table = "typing_printing"


class PrintingReview(Model):
    id = fields.IntField(primary_key=True)
    printing = fields.ForeignKeyField(Printing, related_name="reviews", on_delete=CASCADE)

    class Meta:
        table = "typing_printing_review"


class Note(Model):
    id = fields.IntField(primary_key=True)
    text = fields.TextField()
    subject = fields.GenericForeignKeyField(
        {"volume": Volume, "writer": Writer}, related_name="notes", on_delete=CASCADE
    )

    class Meta:
        table = "typing_note"


class Loan(Model):
    """A relation to the model the ``READER_MODEL`` setting names."""

    id = fields.IntField(primary_key=True)
    reader = fields.ForeignKeyField(swappable("READER_MODEL"), related_name="loans", on_delete=CASCADE)

    class Meta:
        table = "typing_loan"
