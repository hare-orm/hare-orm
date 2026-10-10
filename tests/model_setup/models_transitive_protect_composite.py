"""A composite-primary-key model CASCADE-ing onto a plain model that a PROTECT guard points at -
the transitive-PROTECT check has to carry composite primary keys down the CASCADE hop. The
composite-target FK is db_constraint=False (composite-target FK DDL isn't supported), so this
lives in its own module, like the other composite-target fixtures."""

from hare import fields
from hare.models import Model


class CompositeProtectRoot(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b")


class CompositeProtectMiddle(Model):
    name = fields.TextField()
    root: fields.ForeignKeyRelation[CompositeProtectRoot] = fields.ForeignKeyField(
        "models.CompositeProtectRoot", related_name="middles", on_delete=fields.CASCADE, db_constraint=False
    )


class CompositeProtectGuard(Model):
    name = fields.TextField()
    middle: fields.ForeignKeyRelation[CompositeProtectMiddle] = fields.ForeignKeyField(
        "models.CompositeProtectMiddle", related_name="guards", on_delete=fields.PROTECT
    )
