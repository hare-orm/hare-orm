from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.field import Field
from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.sql.terms.term import Term
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class RowListValueReference:
    """A plan value reference of a list of composite keys (``pk__in``, ``relation__in``) - the
    ``Tuple`` of value rows of ``(a, b) IN ((?, ?), ...)`` and the key's fields. A plan hit binds as
    many rows; the row count is part of the plan key. Publication prepares every column
    converter and replaces the SQL rows with slots before ``bind()`` is called.
    """

    container: Tuple | tuple[tuple[ParameterSlot, ...], ...]
    fields: tuple[Field[Any], ...]
    #: The fields of an instance in the list holding its key, in key order.
    key_names: tuple[str, ...] = ()
    #: The model an instance in the list is of - None for a list of key tuples only.
    instance_class: type[Model] | None = None
    converters: tuple[Callable[[Any, Any], Any], ...] = ()

    def bind(self, parameters: list[Any], value: Any, model: type[Model], dialect: Dialect) -> bool:
        """The parameters of new key rows - each value converted by its key field, as the recorded
        rows were.

        Args:
            parameters: The parameter buffer of this execution, filled in place.
            value: The new rows - key tuples, or instances standing for their keys.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            True when the prepared SQL shape accepts the value; False requests a fresh build.
            Conversion and validation errors propagate to the caller.
        """
        row_terms: tuple[tuple[ParameterSlot, ...], ...] = self.container  # type: ignore[assignment]
        if self.instance_class is not None and isinstance(value, set):
            value = list(value)
        if not isinstance(value, (list, tuple)) or len(value) != len(row_terms):
            return False
        fields = self.fields
        converters = self.converters
        instance_class = self.instance_class
        expression_class = (ExpressionArguments.expression_classes or ExpressionArguments.get_expression_classes())[0]
        unbound_classes = (expression_class, Term)
        for row, row_term in zip(value, row_terms, strict=True):
            if instance_class is not None and isinstance(row, instance_class):
                row = tuple(getattr(row, key_name) for key_name in self.key_names)
            if not isinstance(row, tuple) or len(row) != len(fields):
                return False
            for component, convert, component_term in zip(row, converters, row_term, strict=True):
                if component is None or isinstance(component, unbound_classes):
                    return False
                lookup_value = convert(component, model)
                if isinstance(lookup_value, Term):
                    return False
                if not component_term.bind(parameters, lookup_value):
                    return False
        return True
