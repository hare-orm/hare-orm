from hare import fields
from hare.dialects.postgresql.fields.hstore import HStoreField
from hare.models import Model


class Shelf(Model):
    id = fields.IntField(primary_key=True)


class UniqueAttributes(Model):
    """Rows told apart by their hstore - the conflict a bulk insert skips."""

    id = fields.IntField(primary_key=True)
    tag = fields.CharField(max_length=5)
    attributes = HStoreField(unique=True)


class Item(Model):
    id = fields.IntField(primary_key=True)
    shelf = fields.ForeignKeyField("models.Shelf", related_name="items", null=True)
    attributes = HStoreField(null=True)
