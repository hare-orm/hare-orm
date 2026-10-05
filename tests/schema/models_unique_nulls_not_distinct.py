"""A UniqueConstraint whose NULLs collide - nulls_distinct=False, PostgreSQL 15+ only."""

from __future__ import annotations

from hare import fields
from hare.ddl.constraints import UniqueConstraint
from hare.models import Model


class Shelf(Model):
    id = fields.IntField(primary_key=True)
    row = fields.IntField(null=True)
    place = fields.IntField(null=True)

    class Meta:
        constraints = [UniqueConstraint(fields=("row", "place"), name="shelf_spot", nulls_distinct=False)]
