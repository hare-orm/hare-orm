"""A field named id that is not the primary key, with no other primary key."""

from hare import fields
from hare.models import Model


class Tournament(Model):
    id = fields.CharField(max_length=50)
