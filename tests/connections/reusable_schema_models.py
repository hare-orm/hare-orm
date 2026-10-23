"""The models of the reusable test database tests on keeping a schema from one lease to the next."""

from hare import fields
from hare.models import Model


class KeptCounter(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
