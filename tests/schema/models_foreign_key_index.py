"""Relations covering every case of the default foreign key index."""

from hare import fields
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index, PartialIndex
from hare.models import Model
from hare.query.expressions import Q


class Parent(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "fki_parent"


class Pair(Model):
    left = fields.IntField()
    right = fields.IntField()
    pk = fields.CompositePrimaryKey("left", "right")

    class Meta:
        table = "fki_pair"


class Child(Model):
    id = fields.IntField(primary_key=True)
    code = fields.IntField()
    indexed = fields.ForeignKeyField("models.Parent", related_name="indexed_children")
    unindexed = fields.ForeignKeyField("models.Parent", related_name="unindexed_children", db_index=False)
    unconstrained = fields.ForeignKeyField("models.Parent", related_name="unconstrained_children", db_constraint=False)
    in_unique_together = fields.ForeignKeyField("models.Parent", related_name="unique_together_children")
    in_meta_index = fields.ForeignKeyField("models.Parent", related_name="meta_index_children")
    in_unique_constraint = fields.ForeignKeyField("models.Parent", related_name="unique_constraint_children")
    second_in_index = fields.ForeignKeyField("models.Parent", related_name="second_in_index_children")
    in_partial_index = fields.ForeignKeyField("models.Parent", related_name="partial_index_children")
    single = fields.OneToOneField("models.Parent", related_name="single_child")
    pair = fields.ForeignKeyField("models.Pair", related_name="children")
    unindexed_pair = fields.ForeignKeyField("models.Pair", related_name="unindexed_children", db_index=False)

    class Meta:
        table = "fki_child"
        indexes = (
            Index(fields=("in_meta_index", "code")),
            Index(fields=("code", "second_in_index")),
            PartialIndex(fields=("in_partial_index",), condition=Q(code=1)),
        )
        constraints = (
            UniqueConstraint(fields=("in_unique_together", "code")),
            UniqueConstraint(fields=("in_unique_constraint", "code"), name="fki_child_unique"),
        )


class Tagged(Model):
    id = fields.IntField(primary_key=True)
    tags = fields.ManyToManyField("models.Parent", related_name="tagged", through="fki_tagged_tags")
    repeated_tags = fields.ManyToManyField(
        "models.Parent", related_name="repeated_tagged", through="fki_tagged_repeated", unique=False
    )
    unindexed_tags = fields.ManyToManyField(
        "models.Parent", related_name="unindexed_tagged", through="fki_tagged_unindexed", db_index=False
    )
    pairs = fields.ManyToManyField("models.Pair", related_name="tagged", through="fki_tagged_pairs")

    class Meta:
        table = "fki_tagged"


class Membership(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.ForeignKeyField("models.Parent", related_name="memberships")
    target = fields.ForeignKeyField("models.Pair", related_name="memberships")

    class Meta:
        table = "fki_membership"
        constraints = (UniqueConstraint(fields=("owner", "target")),)
