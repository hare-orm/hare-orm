"""A plain model's ManyToManyField targeting a composite-PK model - the through-table's forward
side gets one column per PK component instead of the usual single shadow column."""

from hare import fields
from hare.models import Model


class CompositeTarget(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b")


class M2MTargetingCompositePk(Model):
    name = fields.TextField()
    others: fields.ManyToManyRelation[CompositeTarget] = fields.ManyToManyField("models.CompositeTarget")
