"""
This is the testing Models — Cyclic
"""

from hare import fields
from hare.models import Model
from tests.schema.models_cyclic import Two


class One(Model):
    tournament: fields.ManyToManyRelation[Two] = fields.ManyToManyField("Two")
