"""Companion model for tests/model_setup/abstract_inheritance/test_abstract_model_inheritance.py - see
meta_collision_mixins.py for the shared abstract mixins."""

from tests.model_setup.abstract_inheritance.meta_collision_mixins import (
    AbstractPriceMixin,
    AbstractQuantityMixin,
)


# mypy flags the two bases' independently-declared Meta classes as incompatible - correct at
# the type level (neither is a subtype of the other), but ModelMeta.__new__ merges them at
# runtime rather than requiring one to win, exactly what this model exists to exercise.
class PriceThenQuantityProduct(AbstractPriceMixin, AbstractQuantityMixin):  # type: ignore[misc]
    pass
