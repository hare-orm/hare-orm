"""
This is the testing Models — Model with field id, but NO PK
"""

from hare import fields
from hare.models import Model


class Tournament(Model):
    id = fields.CharField(max_length=50)
