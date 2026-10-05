"""A ManyToManyField(through=Model, on_delete=SET_NULL) whose through model's own FK field
(left at its own CASCADE default, so the M2M field's SET_NULL is what would get propagated onto
it) isn't null=True - propagating SET_NULL there would violate that field's own requirement."""

from hare import fields
from hare.fields import SET_NULL
from hare.models import Model


class NotNullablePeer(Model):
    name = fields.CharField(max_length=20)


class NotNullableParent(Model):
    name = fields.CharField(max_length=20)
    peers: fields.ManyToManyRelation["NotNullablePeer"] = fields.ManyToManyField(
        "models.NotNullablePeer", through="models.NotNullableMembership", on_delete=SET_NULL
    )


class NotNullableMembership(Model):
    parent = fields.ForeignKeyField(NotNullableParent)
    peer = fields.ForeignKeyField(NotNullablePeer)
