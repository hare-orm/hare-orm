"""A OneToOneField targeting a model with a composite primary key - supported when to_field is
left unset (defaults to the whole composite PK, in order). The owning side's shadow columns get
a composite UniqueConstraint (not a per-column UNIQUE) so the O2O cardinality is enforced on the
combination, not on each column independently."""

from hare import fields
from hare.models import Model


class CompositeTarget(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b")


class O2OTarget(Model):
    other: fields.OneToOneRelation[CompositeTarget] = fields.OneToOneField("models.CompositeTarget")
