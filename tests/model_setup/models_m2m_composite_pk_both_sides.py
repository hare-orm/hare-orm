"""A ManyToManyField where BOTH sides have a composite PK - the through-table needs one column
per PK component on each side (2 on the owner's side, 3 on the target's), independently."""

from hare import fields
from hare.models import Model


class BothSidesCompositeTarget(Model):
    x = fields.IntField()
    y = fields.IntField()
    z = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("x", "y", "z")


class BothSidesCompositeOwner(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    others: fields.ManyToManyRelation[BothSidesCompositeTarget] = fields.ManyToManyField(
        "models.BothSidesCompositeTarget"
    )

    pk = fields.CompositePrimaryKey("a", "b")
