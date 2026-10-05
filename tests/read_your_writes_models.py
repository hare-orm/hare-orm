"""Models of the tests of reads after a write going to the connection written through."""

from hare import fields
from hare.models import Model


class Note(Model):
    id = fields.IntField(primary_key=True)
    text = fields.CharField(max_length=50)


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    notes = fields.ManyToManyField("models.Note", related_name="tags")
