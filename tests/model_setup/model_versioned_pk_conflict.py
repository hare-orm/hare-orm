"""Bad model - Meta.optimistic_lock_field pointing at a column that's also part of the primary key."""

from hare import fields
from hare.contrib.versioning import VersionedModel


class AmbiguousVersioned(VersionedModel):
    title = fields.TextField()

    class Meta:
        optimistic_lock_field = "version"
