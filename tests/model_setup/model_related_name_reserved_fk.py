"""A ForeignKeyField whose related_name shadows a Model method."""

from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)


class Book(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("models.Author", related_name="save")
