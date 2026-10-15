"""A self-referential ManyToManyField(through=Model) - hare cannot auto-detect which of the
through model's two FK fields (both pointing at the same model) belongs to which side."""

from hare import fields
from hare.models import Model


class Node(Model):
    name = fields.CharField(max_length=20)
    friends: fields.ManyToManyRelation["Node"] = fields.ManyToManyField("models.Node", through="models.Edge")


class Edge(Model):
    a = fields.ForeignKeyField(Node, related_name="edges_a")
    b = fields.ForeignKeyField(Node, related_name="edges_b")
