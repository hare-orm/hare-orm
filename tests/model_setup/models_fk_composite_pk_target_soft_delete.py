"""Composite-PK-target FK fixtures for testing on_delete=CASCADE/SET_NULL/SET_DEFAULT via
Model.delete(). Kept in their own module, separate from models_fk_composite_pk_target.py: a hard
delete's CASCADE/SET_NULL/SET_DEFAULT rely on a real DB-level FOREIGN KEY constraint, which a
composite-target FK doesn't have yet (a later, DDL-layer commit) - but Meta.soft_delete_field's
own cascade (ReverseRelationCascade.apply_soft_delete_cascade) reproduces the same on_delete
actions entirely in Python, regardless of db_constraint, so it can be exercised now.

Every model below gets an explicit Meta.table distinct from the other composite-PK-target fixture
modules, kept even after the EXECUTOR_CACHE collision this originally worked around was fixed
(EXECUTOR_CACHE is now keyed by the model class first, not just (connection_name, dialect,
schema, db_table) - two same-named ("CompositeTarget") classes sharing a table can no longer
silently reuse each other's cached insert-column list) - distinct table names are still good
fixture hygiene on their own, independent of that particular bug."""

from hare import fields
from hare.models import Model


class CompositeTarget(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()
    deleted_at = fields.DatetimeField(null=True)

    pk = fields.CompositePrimaryKey("a", "b")

    class Meta:
        table = "composite_target_soft_delete"
        soft_delete_field = "deleted_at"


class FkTargetCascade(Model):
    other: fields.ForeignKeyRelation[CompositeTarget] = fields.ForeignKeyField(
        "models.CompositeTarget", related_name="cascade_children", db_constraint=False
    )

    class Meta:
        table = "fk_target_cascade_soft_delete"


class FkTargetSetNull(Model):
    other: fields.ForeignKeyRelation[CompositeTarget] | None = fields.ForeignKeyField(
        "models.CompositeTarget",
        related_name="set_null_children",
        db_constraint=False,
        null=True,
        on_delete=fields.SET_NULL,
    )

    class Meta:
        table = "fk_target_set_null_soft_delete"


class FkTargetSetDefault(Model):
    other: fields.ForeignKeyRelation[CompositeTarget] = fields.ForeignKeyField(
        "models.CompositeTarget",
        related_name="set_default_children",
        db_constraint=False,
        default=(0, 0),
        on_delete=fields.SET_DEFAULT,
    )

    class Meta:
        table = "fk_target_set_default_soft_delete"
