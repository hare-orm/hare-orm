"""A one-to-one relation whose related_name is a field of its target."""

from hare import fields
from hare.models import Model


class Tournament(Model):
    id = fields.IntField(primary_key=True)
    event = fields.CharField(max_length=32)


class Event(Model):
    tournament: fields.OneToOneRelation[Tournament] = fields.OneToOneField("models.Tournament", related_name="event")
