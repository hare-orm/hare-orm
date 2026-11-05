"""Models for contracts combining hydration, generated values and subsequent writes."""

from hare import fields
from hare.models import Model
from hare.query.expressions import F
from tests.contracts.constants import CONTRACT_APP


class Shelf(Model):
    id = fields.IntField(primary_key=True)


class Book(Model):
    id = fields.IntField(primary_key=True)
    amount = fields.IntField()
    doubled = fields.GeneratedField(F("amount") * 2, output_field=fields.IntField())
    shelf = fields.ForeignKeyField(f"{CONTRACT_APP}.Shelf", db_constraint=False)
