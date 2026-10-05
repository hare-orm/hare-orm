"""
This is the testing Models — Generated non-int PK
"""

from hare import fields
from hare.models import Model


class Tournament(Model):
    val = fields.CharField(max_length=50, primary_key=True, generated=True)
