"""Single and composite identities for mutable filter and JSON write inputs."""

from hare import fields
from hare.models import Model


class SnapshotItem(Model):
    id = fields.IntField(primary_key=True, generated=False)
    payload = fields.JSONTextField()


class CompositeSnapshotItem(Model):
    group = fields.IntField()
    number = fields.IntField()
    payload = fields.JSONTextField()
    pk = fields.CompositePrimaryKey("group", "number")
