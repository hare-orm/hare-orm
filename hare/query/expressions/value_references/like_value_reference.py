from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.filters.lookups.lookups import Lookups
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class LikeValueReference:
    """A plan value reference of ``__contains``/``__startswith``/``__endswith`` and their insensitive variants: the
    pattern's parameter slot, the encoder converting the raw value, the field, and the
    ``prefix``/``suffix`` wildcard flags. A plan hit encodes the new value and builds its escaped
    pattern again. Case-insensitive lookups share the same source inside their SQL case conversion.
    Preparation identifies a ``ValueWrapper``; publication replaces it with a ``ParameterSlot``.
    """

    wrapper: ParameterSlot | ValueWrapper
    field: Field[Any]
    value_encoder: Callable[..., str]
    prefix: bool
    suffix: bool

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The pattern parameter of a new value - encoded, then escaped and wrapped in the
        recorded wildcards.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        encoded = self.value_encoder(value, model, self.field, dialect)
        pattern = Lookups.get_like_pattern_text(encoded, prefix=self.prefix, suffix=self.suffix)
        return self.wrapper.bind(parameters, pattern, False)  # type: ignore[union-attr]
