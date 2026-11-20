from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class ListValueReference:
    """A plan value reference of ``__in``/``__not_in``: the parameter slots, the field and the
    encoder converting the list. A plan hit runs the encoder on the new list - its length is part of
    the plan key. Preparation holds the source SQL ``Tuple``; publication replaces it with
    a tuple of immutable ``ParameterSlot`` objects. Only published references are bound.
    """

    container: Tuple | tuple[ParameterSlot, ...]
    field: Field[Any]
    value_encoder: Callable[..., list[Any]]

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameters of a new list - encoded the way the recorded one was, one per item.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new list.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        if not isinstance(value, (list, tuple, set)):
            return False
        targets: tuple[ParameterSlot, ...] = self.container  # type: ignore[assignment]
        # A None in the list is an IS NULL test the plan's text holds - only the other values bind.
        encoded_values = self.value_encoder(value, model, self.field, dialect)
        if any(element is None for element in encoded_values):
            encoded_values = [element for element in encoded_values if element is not None]
        if len(encoded_values) != len(targets):
            return False
        if any(isinstance(encoded_value, Term) for encoded_value in encoded_values):
            return False
        for target, encoded_value in zip(targets, encoded_values, strict=True):
            if not target.bind(parameters, encoded_value, True):
                return False
        return True
