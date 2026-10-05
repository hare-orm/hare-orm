"""A ForeignKeyField whose related_name is no attribute name."""

from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)


class Book(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("models.Author", related_name="not valid")
