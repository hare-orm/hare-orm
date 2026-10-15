"""Models for tests/test_related_managers.py - related managers of backward foreign keys and
many-to-many relations, to single-column and composite primary keys."""

from hare import fields
from hare.models import Model


class Shelf(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "rm_shelf"


class Item(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    shelf = fields.ForeignKeyField(Shelf, related_name="items", null=True)

    class Meta:
        table = "rm_item"


class Label(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    shelf = fields.ForeignKeyField(Shelf, related_name="labels")

    class Meta:
        table = "rm_label"


class HiddenItem(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    deleted_at = fields.DatetimeField(null=True)
    shelf = fields.ForeignKeyField(Shelf, related_name="hidden_items", null=True)

    class Meta:
        table = "rm_hidden_item"
        soft_delete_field = "deleted_at"


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    shelves = fields.ManyToManyField(Shelf, related_name="tags")

    class Meta:
        table = "rm_tag"


class Bay(Model):
    row = fields.IntField()
    slot = fields.IntField()
    name = fields.CharField(max_length=20)
    pk = fields.CompositePrimaryKey("row", "slot")

    class Meta:
        table = "rm_bay"


class OrderedBay(Model):
    row = fields.IntField()
    slot = fields.IntField()
    name = fields.CharField(max_length=20)
    pk = fields.CompositePrimaryKey("row", "slot")

    class Meta:
        table = "rm_ordered_bay"
        ordering = ("-pk",)


class Crate(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    bay = fields.ForeignKeyField(Bay, related_name="crates", null=True)

    class Meta:
        table = "rm_crate"


class Sticker(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    bays = fields.ManyToManyField(Bay, related_name="stickers")

    class Meta:
        table = "rm_sticker"


class SoftBay(Model):
    row = fields.IntField()
    slot = fields.IntField()
    name = fields.CharField(max_length=20)
    deleted_at = fields.DatetimeField(null=True)
    pk = fields.CompositePrimaryKey("row", "slot")

    class Meta:
        table = "rm_soft_bay"
        soft_delete_field = "deleted_at"


class SoftBayCrate(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
    bay = fields.ForeignKeyField(SoftBay, related_name="crates", null=True, on_delete=fields.NO_ACTION)

    class Meta:
        table = "rm_soft_bay_crate"
