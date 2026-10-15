from hare import fields
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class Writer(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "slice_writer"


class WriterProfile(Model):
    id = fields.IntField(primary_key=True)
    writer = fields.OneToOneField(Writer, related_name="profile")

    class Meta:
        table = "slice_writer_profile"


class Label(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "slice_label"
        ordering = ("name",)


class Novel(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=20)
    rating = fields.IntField()
    writer = fields.ForeignKeyField(Writer, related_name="novels")
    labels = fields.ManyToManyField(Label, related_name="novels")

    class Meta:
        table = "slice_novel"


class Chapter(Model):
    id = fields.IntField(primary_key=True)
    number = fields.IntField()
    novel = fields.ForeignKeyField(Novel, related_name="chapters")

    class Meta:
        table = "slice_chapter"


class Shipment(Model):
    """A composite primary key - the parent of a sliced reverse relation and a many-to-many."""

    pk = CompositePrimaryKey("store", "number")
    store = fields.IntField()
    number = fields.IntField()
    labels = fields.ManyToManyField(Label, related_name="shipments")

    class Meta:
        table = "slice_shipment"


class Parcel(Model):
    pk = CompositePrimaryKey("box", "slot")
    box = fields.IntField()
    slot = fields.IntField()
    weight = fields.IntField()
    shipment = fields.ForeignKeyField(Shipment, related_name="parcels")

    class Meta:
        table = "slice_parcel"
