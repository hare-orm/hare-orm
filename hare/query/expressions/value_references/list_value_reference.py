from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class ListValueReference:
    """A plan value reference of ``__in``/``__not_in``: the ``Tuple`` of the values, the field and the
    encoder converting the list. A plan hit runs the encoder on the new list - its length is part of
    the plan key.
    """

    container: Tuple
    field: Field[Any]
    value_encoder: Callable[..., list[Any]]

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameters of a new list - encoded the way the recorded one was, one per item.

        Args:
            value: The new list.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters, or None for a value that isn't a list of the recorded number of values
            other than None, or an item that encodes to a term.
        """
        if not isinstance(value, (list, tuple, set)):
            return None
        # A None in the list is an IS NULL test the plan's text holds - only the other values bind.
        encoded_values = [
            encoded_value
            for encoded_value in self.value_encoder(value, model, self.field, dialect)
            if encoded_value is not None
        ]
        if len(encoded_values) != len(self.container.values):
            return None
        if any(isinstance(encoded_value, Term) for encoded_value in encoded_values):
            return None
        return [
            (id(old_wrapper), encoded_value, True)
            for old_wrapper, encoded_value in zip(self.container.values, encoded_values, strict=True)
        ]
