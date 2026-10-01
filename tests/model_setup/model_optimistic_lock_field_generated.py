"""Bad model - Meta.optimistic_lock_field pointing at a DB-generated column."""

from hare import fields
from hare.models import Model


class GeneratedVersion(Model):
    id = fields.IntField(primary_key=True)
    version = fields.IntField(generated=True)

    class Meta:
        managed = False
        optimistic_lock_field = "version"
