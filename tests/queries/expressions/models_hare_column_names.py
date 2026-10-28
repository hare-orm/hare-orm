"""A model whose columns are named as the columns hare selects itself next to them."""

from hare import fields
from hare.models import Model


class SpacedColumns(Model):
    id = fields.IntField(primary_key=True, source_field="hare depth")
    app = fields.CharField(max_length=20, default="", source_field="hare app")
    model_name = fields.CharField(max_length=20, default="", source_field="hare model")
    window = fields.IntField(default=0, source_field="hare window filter 0")
    ordering = fields.IntField(default=0, source_field="hare distinct ordering 0")
    inserted = fields.IntField(default=0, source_field="hare row inserted")
    matches = fields.IntField(default=0, source_field="hare matches filter")
    old_id = fields.IntField(default=0, source_field="hare old hare depth")
    parent = fields.ForeignKeyField(
        "models.SpacedColumns", related_name="children", null=True, db_constraint=False, source_field="parent id"
    )


class InsertedFlagNamedKey(Model):
    """Its key column named as the flag an upsert returns beside the key."""

    id = fields.IntField(primary_key=True, source_field="hare row inserted")
    name = fields.CharField(max_length=20)


class UpperCaseDepthNode(Model):
    """Its key columns named like the depth hare selects beside them, in other letter case."""

    a = fields.IntField(source_field="HARE DEPTH")
    b = fields.IntField(source_field="Hare Depth 2")
    parent = fields.ForeignKeyField(
        "models.UpperCaseDepthNode", related_name="children", null=True, db_constraint=False
    )
    pk = fields.CompositePrimaryKey("a", "b")
