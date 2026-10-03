"""A ManyToManyField(through=Model, on_delete=SET_DEFAULT) whose through model's own FK field
(left at its own CASCADE default, so the M2M field's SET_DEFAULT is what would get propagated onto
it) has only a Python-side default= - with a real FK constraint the database itself performs
ON DELETE SET DEFAULT, which never sees default=, so propagating SET_DEFAULT there is rejected
the same way a ForeignKeyField(on_delete=SET_DEFAULT, default=...) declared directly would be."""

from hare import fields
from hare.fields import SET_DEFAULT
from hare.models import Model


class OnlyDefaultPeer(Model):
    name = fields.CharField(max_length=20)


class OnlyDefaultParent(Model):
    name = fields.CharField(max_length=20)
    peers: fields.ManyToManyRelation["OnlyDefaultPeer"] = fields.ManyToManyField(
        "models.OnlyDefaultPeer", through="models.OnlyDefaultMembership", on_delete=SET_DEFAULT
    )


class OnlyDefaultMembership(Model):
    parent = fields.ForeignKeyField(OnlyDefaultParent, default=1)
    peer = fields.ForeignKeyField(OnlyDefaultPeer)
