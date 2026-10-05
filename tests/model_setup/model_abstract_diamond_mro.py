"""Companion model for test_abstract_model_inheritance.py's diamond-MRO regression test.

DiamondConcrete's real Python MRO is [DiamondConcrete, Mixin1, Mixin2, AbstractA, Model, ...] -
Mixin1 is listed first in `bases` but passively inherits `shared`/Meta.table from AbstractA
without overriding either, while Mixin2 (listed second) genuinely overrides both. ModelMeta used
to resolve both to whichever value AbstractA itself declared (protracted through Mixin1, the
non-overriding base, simply because Mixin1 was walked first) instead of Mixin2's own override,
even though Mixin2 is the nearer declaration in the real MRO.
"""

from hare import fields
from hare.models import Model


class AbstractA(Model):
    shared = fields.CharField(max_length=10)

    class Meta:
        abstract = True
        table = "from_abstract_a"


class Mixin1(AbstractA):
    class Meta:
        abstract = True

    # Does not override `shared` or Meta.table - passively inherits both from AbstractA.


class Mixin2(AbstractA):
    shared = fields.CharField(max_length=99)

    class Meta:
        abstract = True
        table = "from_mixin2"


class DiamondConcrete(Mixin1, Mixin2):
    pass
