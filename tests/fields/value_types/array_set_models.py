"""Array values supplied through constructors and defaults, with either primary-key shape."""

from hare import fields
from hare.models import Model


class ArraySetRecord(Model):
    id = fields.IntField(primary_key=True, generated=False)
    numbers = fields.ArrayField(fields.IntField())
    nested_numbers = fields.ArrayField(fields.ArrayField(fields.IntField()))
    default_numbers = fields.ArrayField(fields.IntField(), default={1, 6, 10})


class CompositeArraySetRecord(Model):
    batch = fields.IntField()
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("batch", "number")
    numbers = fields.ArrayField(fields.IntField())
    nested_numbers = fields.ArrayField(fields.ArrayField(fields.IntField()))
    default_numbers = fields.ArrayField(fields.IntField(), default={1, 6, 10})
