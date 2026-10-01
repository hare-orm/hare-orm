"""A ForeignKeyField targeting a composite-PK model with the default db_constraint=True - kept
in its own module, separate from models_fk_composite_pk_target.py's other fixtures. Its
REFERENCES clause is still wrong/incomplete (a real composite table-level FOREIGN KEY constraint
is a later, DDL-layer commit - see the schema-shape test that uses this module), and SQLite
validates FK definitions for every table referencing a row being deleted, not just the one being
written to - sharing a module with fixtures that do real deletes against CompositeTarget would
make every one of those deletes fail on THIS model's still-broken schema instead of their own."""

from hare import fields
from hare.models import Model


class CompositeTarget(Model):
    a = fields.IntField()
    b = fields.IntField()
    name = fields.TextField()

    pk = fields.CompositePrimaryKey("a", "b")


class FkTarget(Model):
    other: fields.ForeignKeyRelation[CompositeTarget] = fields.ForeignKeyField("models.CompositeTarget")
