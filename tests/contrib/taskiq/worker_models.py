"""The models of the worker HareTaskiq sets up in tests/contrib/taskiq/ - apart from the other
tests' models, which the worker's configuration would bind to its own connection."""

from hare import fields
from hare.models import Model


class WorkerNote(Model):
    id = fields.IntField(primary_key=True)
