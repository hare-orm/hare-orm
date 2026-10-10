"""The event model on hare - a module of its own, as hare finds its models by scanning a module."""

from __future__ import annotations

from hare import fields
from hare.models import Model


class Event(Model):
    id = fields.UUIDField(primary_key=True)
    site = fields.CharField(max_length=50)
    user_id = fields.IntField()
    amount = fields.FloatField()
    happened_at = fields.DatetimeField()
    event_type = fields.CharField(max_length=20)

    class Meta:
        table = "event"
        app = "models"
