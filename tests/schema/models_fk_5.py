"""
This is the testing Models — on_delete SET_DEFAULT with db_default set (valid)
"""

from hare import fields
from hare.fields.db_defaults import SqlDefault
from hare.models import Model


class Two(Model):
    id = fields.IntField(primary_key=True)


class One(Model):
    tournament: fields.ForeignKeyRelation[Two] = fields.ForeignKeyField(
        "models.Two", on_delete=fields.SET_DEFAULT, db_default=SqlDefault("1")
    )
