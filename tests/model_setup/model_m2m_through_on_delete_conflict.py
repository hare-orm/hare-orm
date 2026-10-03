"""A ManyToManyField(through=Model) whose own on_delete conflicts with the through model's own
FK field's explicitly-declared on_delete - ambiguous, hare has no way to reconcile the two."""

from hare import fields
from hare.fields import PROTECT, SET_NULL
from hare.models import Model


class ConflictPeer(Model):
    name = fields.CharField(max_length=20)


class ConflictParent(Model):
    name = fields.CharField(max_length=20)
    peers: fields.ManyToManyRelation["ConflictPeer"] = fields.ManyToManyField(
        "models.ConflictPeer", through="models.ConflictMembership", on_delete=SET_NULL
    )


class ConflictMembership(Model):
    parent = fields.ForeignKeyField(ConflictParent, on_delete=PROTECT, null=True)
    peer = fields.ForeignKeyField(ConflictPeer)
