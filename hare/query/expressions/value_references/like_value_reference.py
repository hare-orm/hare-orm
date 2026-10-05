from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.filters.lookups.lookups import Lookups
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class LikeValueReference:
    """A plan value reference of a case-sensitive ``__contains``/``__startswith``/``__endswith``: the
    pattern's ``ValueWrapper``, the encoder converting the raw value, the field, and the
    ``prefix``/``suffix`` wildcard flags. A plan hit encodes the new value and builds its escaped
    pattern again. The case-insensitive lookups wrap the pattern in ``UPPER(...)`` and aren't
    referenced this way.
    """

    wrapper: ValueWrapper
    field: Field[Any]
    value_encoder: Callable[..., str]
    prefix: bool
    suffix: bool

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The pattern parameter of a new value - encoded, then escaped and wrapped in the
        recorded wildcards.

        Args:
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters.
        """
        encoded = self.value_encoder(value, model, self.field, dialect)
        pattern = Lookups.get_like_pattern_text(encoded, prefix=self.prefix, suffix=self.suffix)
        return [(id(self.wrapper), pattern, False)]
