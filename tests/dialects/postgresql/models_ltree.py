from __future__ import annotations

from hare import Model, fields
from hare.dialects.postgresql.fields.ltree_field import LtreeField
from hare.dialects.postgresql.indexes import GistIndex


class TreeCategory(Model):
    id = fields.IntField(primary_key=True)
    path = LtreeField(unique=True)
    parent_path = LtreeField(null=True)

    class Meta:
        table = "ltree_tree_category"
        indexes = (GistIndex(fields=("path",)),)
