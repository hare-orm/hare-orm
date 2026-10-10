"""A self-referential ManyToManyField on a composite-PK model - the auto-defaulted backward
through-table columns would otherwise collide with the forward ones (both sides are the same
model/PK shape), same as the existing single-column self-referential case."""

from hare import fields
from hare.models import Model


class CompositePkPerson(Model):
    org_id = fields.IntField()
    person_id = fields.IntField()
    name = fields.TextField()
    friends: fields.ManyToManyRelation["CompositePkPerson"] = fields.ManyToManyField("models.CompositePkPerson")

    pk = fields.CompositePrimaryKey("org_id", "person_id")
