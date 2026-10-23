"""
This is the testing Models — on_delete SET_DEFAULT without default/db_default
"""

from hare import fields
from hare.models import Model
from tests.schema.models_cyclic import Two


class One(Model):
    tournament: fields.ForeignKeyRelation[Two] = fields.ForeignKeyField("models.Two", on_delete=fields.SET_DEFAULT)
