"""Companion models for tests/model_setup/test_abstract_model_inheritance.py.

GeneratedField declared on an abstract base, inherited by two concrete siblings - the DDL/
expression must be generated correctly and independently for each concrete subclass's own table
(not aliased/memoized against whichever sibling's schema gets created first, the same class of
bug round M already fixed for Index.get_expressions())."""

from hare import fields
from hare.fields.generated import GeneratedField
from hare.models import Model


class AbstractGeneratedBase(Model):
    quantity = fields.IntField()
    unit_price = fields.IntField()
    total = GeneratedField(expression="quantity * unit_price", output_field=fields.IntField())

    class Meta:
        abstract = True


class GeneratedSiblingA(AbstractGeneratedBase):
    pass


class GeneratedSiblingB(AbstractGeneratedBase):
    pass
