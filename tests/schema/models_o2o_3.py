"""
This is the testing Models — on_delete SET_NULL without null=True
"""

from hare import fields
from hare.models import Model
from tests.schema.models_cyclic import Two


class One(Model):
    tournament: fields.OneToOneRelation[Two] = fields.OneToOneField("models.Two", on_delete=fields.SET_NULL)
