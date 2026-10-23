"""Models for the tests of a table without a primary key."""

from hare import fields
from hare.models import Model


class Venue(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class VisitLog(Model):
    """One visit - an append-only log row."""

    venue: fields.ForeignKeyRelation[Venue] = fields.ForeignKeyField(
        "models.Venue", related_name="visits", on_delete=fields.CASCADE
    )
    visitor = fields.CharField(max_length=50)
    duration = fields.IntField(default=0)
    visited_at = fields.DatetimeField(auto_now_add=True)
    details = fields.JSONField(null=True)

    class Meta:
        primary_key = None
        ordering = ("visitor",)


class VisitNote(Model):
    """A note on a venue its relation leaves to hare - no FOREIGN KEY in the database."""

    venue: fields.ForeignKeyRelation[Venue] = fields.ForeignKeyField(
        "models.Venue", related_name="notes", on_delete=fields.CASCADE, db_constraint=False
    )
    text = fields.CharField(max_length=50)

    class Meta:
        primary_key = None
