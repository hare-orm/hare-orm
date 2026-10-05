"""Models of the ClickHouse transaction tests - tables of the MergeTree family, which transactions take."""

from hare import fields
from hare.models import Model


class Entry(Model):
    """An entry of a ledger."""

    id = fields.BigIntField(primary_key=True, generated=False)
    account = fields.CharField(max_length=20)
    amount = fields.IntField()


class Note(Model):
    """A note of an entry - deleted with it."""

    id = fields.BigIntField(primary_key=True, generated=False)
    entry: fields.ForeignKeyRelation[Entry] = fields.ForeignKeyField("models.Entry", related_name="notes")
    text = fields.TextField()
