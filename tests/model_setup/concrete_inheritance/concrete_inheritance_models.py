"""A concrete model subclassing a concrete model a relation points at."""

from hare import Model, fields


class Shelf(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=10, default="shelf")


class CornerShelf(Shelf):
    """A table of its own - no row of a relation to Shelf is its."""

    corner = fields.IntField(default=0)


class ShelfItem(Model):
    id = fields.IntField(primary_key=True)
    shelf = fields.ForeignKeyField("models.Shelf", related_name="items", null=True)
