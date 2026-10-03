"""
This is the testing Models — on_delete SET_DEFAULT with only a Python-side default= on a real FK constraint
"""

from hare import fields
from hare.models import Model


class Two(Model):
    id = fields.IntField(primary_key=True)


class One(Model):
    tournament: fields.ForeignKeyRelation[Two] = fields.ForeignKeyField(
        "models.Two", on_delete=fields.SET_DEFAULT, default=1
    )
