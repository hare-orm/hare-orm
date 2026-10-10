"""
This is the testing Models — FK bad model name
"""

from __future__ import annotations

from typing import Any

from hare import fields
from hare.models import Model


class One(Model):
    tournament: fields.ForeignKeyRelation[Any] = fields.ForeignKeyField("moo")
