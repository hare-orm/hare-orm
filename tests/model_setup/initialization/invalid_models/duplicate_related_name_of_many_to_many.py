"""Two many-to-many relations to one model with the same related_name."""

from __future__ import annotations

from hare import fields
from hare.models import Model


class Event(Model):
    participants: fields.ManyToManyRelation[Team] = fields.ManyToManyField(
        "models.Team", related_name="events", through="event_team"
    )


class Party(Model):
    participants: fields.ManyToManyRelation[Team] = fields.ManyToManyField(
        "models.Team", related_name="events", through="event_team"
    )


class Team(Model):
    id = fields.IntField(primary_key=True)
