"""Two models whose foreign keys point at each other - a cycle generate_schemas() breaks."""

from __future__ import annotations

from hare import fields
from hare.models import Model


class CycleHead(Model):
    id = fields.IntField(primary_key=True)
    tail: fields.ForeignKeyNullableRelation[CycleTail] = fields.ForeignKeyField(
        "models.CycleTail", related_name="heads", null=True, on_delete=fields.SET_NULL
    )


class CycleTail(Model):
    id = fields.IntField(primary_key=True)
    head: fields.ForeignKeyNullableRelation[CycleHead] = fields.ForeignKeyField(
        "models.CycleHead", related_name="tails", null=True, on_delete=fields.SET_NULL
    )
