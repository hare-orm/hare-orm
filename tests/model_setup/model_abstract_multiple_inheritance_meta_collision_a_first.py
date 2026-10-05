"""Companion model for tests/model_setup/test_abstract_model_inheritance.py - see
model_abstract_multiple_inheritance_meta_collision.py for the shared abstract mixins."""

from tests.model_setup.model_abstract_multiple_inheritance_meta_collision import (
    AbstractPriceMixin,
    AbstractQuantityMixin,
)


# mypy flags the two bases' independently-declared Meta classes as incompatible - correct at
# the type level (neither is a subtype of the other), but ModelMeta.__new__ merges them at
# runtime rather than requiring one to win, exactly what this model exists to exercise.
class QuantityThenPriceProduct(AbstractQuantityMixin, AbstractPriceMixin):  # type: ignore[misc]
    pass
