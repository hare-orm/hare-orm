from hare import fields
from hare.dialects.postgresql.fields.hstore import HStoreField
from hare.models import Model


class Shelf(Model):
    id = fields.IntField(primary_key=True)


class Item(Model):
    id = fields.IntField(primary_key=True)
    shelf = fields.ForeignKeyField("models.Shelf", related_name="items", null=True)
    attributes = HStoreField(null=True)
