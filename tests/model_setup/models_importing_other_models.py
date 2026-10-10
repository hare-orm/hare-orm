"""A models module declaring one model and importing others from another models module."""

from hare import fields
from hare.models import Model
from tests.testmodels import Author, Book

__all__ = ["Author", "Book", "ImportingWidget"]


class ImportingWidget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)
