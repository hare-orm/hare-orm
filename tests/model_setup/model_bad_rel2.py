"""
Testing Models for a bad/wrong relation reference
The model 'Tour' does not exist
"""

from __future__ import annotations

from typing import Any

from hare import fields
from hare.models import Model


class Tournament(Model):
    id = fields.IntField(primary_key=True)


class Event(Model):
    tournament: fields.ForeignKeyRelation[Any] = fields.ForeignKeyField("models.Tour", related_name="events")
