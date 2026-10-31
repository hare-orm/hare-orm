"""A model with an optimistic lock whose key column is named as a column hare reads beside it."""

from hare import fields
from hare.models import Model


class FilterFlagNamedKey(Model):
    """Its key column named as the flag a filtered bulk_update() reads beside the key - to tell a row its
    filter left out from a stale one, which only a model with an optimistic lock asks."""

    id = fields.IntField(primary_key=True, source_field="hare matches filter")
    value = fields.IntField()
    version = fields.IntField(default=0)

    class Meta:
        optimistic_lock_field = "version"
