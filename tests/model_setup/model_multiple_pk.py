"""
This is the testing Models — Multiple PK
"""

from hare import fields
from hare.models import Model


class Tournament(Model):
    id = fields.IntField(primary_key=True)
    id2 = fields.IntField(primary_key=True)
