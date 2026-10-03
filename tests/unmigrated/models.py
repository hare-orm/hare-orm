"""The models of an app with no migrations package."""

from hare import fields
from hare.models import Model


class UnmigratedNote(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)
