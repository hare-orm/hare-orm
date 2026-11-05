"""Mutable Meta declarations inherited by sibling models keep independent snapshots."""

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.models import Model


class AbstractMetaOptionsBase(Model):
    name = fields.CharField(50)
    category = fields.CharField(20)
    quantity = fields.IntField()

    class Meta:
        abstract = True
        ordering = ("-quantity", "name")
        constraints = (
            UniqueConstraint(fields=("category", "name")),
            CheckConstraint(check=RawSQLTerm("quantity >= 0"), name="quantity_non_negative"),
        )


class MetaOptionsSiblingA(AbstractMetaOptionsBase):
    pass


class MetaOptionsSiblingB(AbstractMetaOptionsBase):
    pass
