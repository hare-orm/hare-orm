"""Inherited generated fields bind independently to each concrete model and its columns."""

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields.generated_field import GeneratedField
from hare.models import Model


class AbstractGeneratedBase(Model):
    quantity = fields.IntField()
    unit_price = fields.IntField()
    total = GeneratedField(expression=RawSQLTerm("quantity * unit_price"), output_field=fields.IntField())

    class Meta:
        abstract = True


class GeneratedSiblingA(AbstractGeneratedBase):
    pass


class GeneratedSiblingB(AbstractGeneratedBase):
    pass
