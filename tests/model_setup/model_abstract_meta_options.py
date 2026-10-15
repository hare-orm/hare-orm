"""Companion models for tests/model_setup/test_abstract_model_inheritance.py.

Meta.ordering / Meta.unique_together / Meta.constraints (CheckConstraint) declared on an
abstract base and inherited, aliased (not deep-copied) by ModelMeta, by two concrete siblings -
round M already fixed the same class of bug for Meta.indexes (Index objects memoize their own
expression resolution in place against whichever model calls get_expressions() first)."""

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
