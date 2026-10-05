"""
Testing Models for a related_name collision with no %(app_label)s/%(class)s template - the
resulting ConfigurationError must suggest templating as the fix.
"""

from hare import fields
from hare.models import Model


class Tournament(Model):
    id = fields.IntField(primary_key=True)


class Event(Model):
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField(
        "models.Tournament", related_name="events"
    )


class Party(Model):
    tournament: fields.ForeignKeyRelation[Tournament] = fields.ForeignKeyField(
        "models.Tournament", related_name="events"
    )
