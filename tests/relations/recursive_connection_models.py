"""Self-referencing models sharing the schema of independent connections."""

from hare import fields
from hare.models import Model


class TreeNode(Model):
    _raw_extra_default = 0
    name = fields.TextField()
    parent = fields.ForeignKeyField("models.TreeNode", related_name="children", null=True, db_constraint=False)


class CompositeTreeNode(Model):
    _raw_extra_default = 0
    number = fields.IntField()
    revision = fields.IntField()
    name = fields.TextField()
    parent = fields.ForeignKeyField(
        "models.CompositeTreeNode", related_name="children", null=True, db_constraint=False
    )
    pk = fields.CompositePrimaryKey("number", "revision")
