"""
This is the testing Models — ManyToMany with on_delete=SET_DEFAULT (unsupported)
"""

from hare import fields
from hare.models import Model
from tests.schema.models_cyclic import Two


class One(Model):
    tournament: fields.ManyToManyRelation[Two] = fields.ManyToManyField("models.Two", on_delete=fields.SET_DEFAULT)
