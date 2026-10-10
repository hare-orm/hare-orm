"""Models of tests/contrib/taskiq/."""

from hare import fields
from hare.contrib.outbox import OutboxEvent
from hare.models import Model


class TaskiqOutboxEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        pass


class TaskNote(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
