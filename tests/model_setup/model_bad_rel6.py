"""
Testing Models for a bad/wrong relation reference
Wrong reference. fk field parameter `to_field` with non unique field.
"""

from hare import fields
from hare.models import Model


class Tournament(Model):
    uuid = fields.UUIDField(unique=False)


class Event(Model):
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField(
        "models.Tournament", related_name="events", to_field="uuid"
    )
