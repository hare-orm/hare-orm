"""A DB-generated primary key that is no integer."""

from hare import fields
from hare.models import Model


class Tournament(Model):
    val = fields.CharField(max_length=50, primary_key=True, generated=True)
