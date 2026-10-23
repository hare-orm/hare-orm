"""Same shape as the only-default fixture, but the through model carries a
db_default on both of its FK fields (the M2M field's on_delete is propagated onto each side) - the
real ON DELETE SET DEFAULT constraint can fall back to it, so the propagation is valid."""

from hare import fields
from hare.fields import SET_DEFAULT
from hare.models import Model


class DbDefaultPeer(Model):
    name = fields.CharField(max_length=20)


class DbDefaultParent(Model):
    name = fields.CharField(max_length=20)
    peers: fields.ManyToManyRelation["DbDefaultPeer"] = fields.ManyToManyField(
        "models.DbDefaultPeer", through="models.DbDefaultMembership", on_delete=SET_DEFAULT
    )


class DbDefaultMembership(Model):
    parent = fields.ForeignKeyField(DbDefaultParent, db_default=1)
    peer = fields.ForeignKeyField(DbDefaultPeer, db_default=1)
