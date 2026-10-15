"""Shared abstract mixins for model_abstract_multiple_inheritance_meta_collision_a_first.py and
model_abstract_multiple_inheritance_meta_collision_b_first.py - each concrete model there
inherits from BOTH, in opposite orders. Kept in their own file (not alongside a concrete model)
so neither companion module ever registers both concrete models on the same app at once - each
inherits the exact same explicitly-named Index from these mixins, which apps.py's own
cross-model collision guard correctly rejects if two concrete models sharing it were ever
registered on the same connection together, a fixture detail unrelated to what these tests
exercise."""

from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import Index
from hare.fields import IntField
from hare.models import Model


class AbstractQuantityMixin(Model):
    qty = IntField()

    class Meta:
        abstract = True
        constraints = (
            UniqueConstraint(fields=("qty",)),
            CheckConstraint(check=RawSQLTerm("qty >= 0"), name="qty_nonneg"),
        )
        indexes = [Index(fields=("qty",), name="idx_multi_inherit_qty")]


class AbstractPriceMixin(Model):
    price = IntField()

    class Meta:
        abstract = True
        constraints = (
            UniqueConstraint(fields=("price",)),
            CheckConstraint(check=RawSQLTerm("price >= 0"), name="price_nonneg"),
        )
        indexes = [Index(fields=("price",), name="idx_multi_inherit_price")]
