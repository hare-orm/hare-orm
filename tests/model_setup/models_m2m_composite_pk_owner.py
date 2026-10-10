"""A composite-PK model declaring its own ManyToManyField - the through-table's backward side
gets one column per PK component instead of the usual single shadow column."""

from hare import fields
from hare.models import Model


class PlainTarget(Model):
    name = fields.TextField()


class CompositePkM2MOwner(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    others: fields.ManyToManyRelation[PlainTarget] = fields.ManyToManyField("models.PlainTarget")

    pk = fields.CompositePrimaryKey("a", "b")
