from hare import fields
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class CompositeTextKeyEntry(Model):
    region = fields.CharField(max_length=64)
    account = fields.CharField(max_length=64)
    entry_key = fields.CharField(max_length=128)
    data = fields.JSONField(default=dict)
    pk = CompositePrimaryKey("region", "account", "entry_key")

    class Meta:
        table = "composite_text_key_entry"


class CompositeIntKeyEntry(Model):
    first = fields.IntField()
    second = fields.IntField()
    label = fields.CharField(max_length=20, default="")
    pk = CompositePrimaryKey("first", "second")

    class Meta:
        table = "composite_int_key_entry"
