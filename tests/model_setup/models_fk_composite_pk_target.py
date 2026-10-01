"""A ForeignKeyField targeting a model with a composite primary key - supported when to_field
is left unset (defaults to the whole composite PK, in order). Every model here uses
db_constraint=False - a composite-target FK's own DDL (a real table-level FOREIGN KEY
constraint) is a separate, later commit; see models_fk_composite_pk_target_default_constraint.py
for the db_constraint=True structural-only fixture (kept in its own module so its still-broken
schema can't break a real DELETE against CompositeTarget here - see that module's docstring)."""

from hare import fields
from hare.models import Model


class CompositeTarget(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b")


class FkTargetNoConstraint(Model):
    """db_constraint=False - a real, already-supported way to skip the inline REFERENCES clause
    entirely. Composite-target FK DDL (a real table-level FOREIGN KEY constraint) is a separate,
    later commit - this model lets JOIN/filter/select_related/prefetch tests insert and query
    real rows against a real table now, without depending on that DDL landing first."""

    other: fields.ForeignKeyRelation[CompositeTarget] | None = fields.ForeignKeyField(
        "models.CompositeTarget", related_name="no_constraint_children", db_constraint=False, null=True
    )
    name = fields.TextField()


class O2OTargetNoConstraint(Model):
    """db_constraint=False O2O counterpart of FkTargetNoConstraint - lets reverse-O2O prefetch
    tests insert and query real rows without the composite-target FK's own DDL."""

    other: fields.OneToOneRelation[CompositeTarget] = fields.OneToOneField(
        "models.CompositeTarget", related_name="no_constraint_o2o_child", db_constraint=False
    )
    name = fields.TextField()


class FkTargetProtect(Model):
    """on_delete=PROTECT against a composite-target FK - deleting the target must be blocked
    while a protecting row exists. PROTECT is checked by Model.delete()/QuerySet.delete()
    unconditionally, before either the soft- or hard-delete path runs, so (unlike
    CASCADE/SET_NULL/SET_DEFAULT - see models_fk_composite_pk_target_soft_delete.py) it doesn't
    need Meta.soft_delete_field to be exercisable without the composite-target FK's own DDL."""

    other: fields.ForeignKeyRelation[CompositeTarget] = fields.ForeignKeyField(
        "models.CompositeTarget", related_name="protect_children", db_constraint=False, on_delete=fields.PROTECT
    )
