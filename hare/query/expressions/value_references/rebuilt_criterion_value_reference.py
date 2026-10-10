from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.exceptions import HareError
from hare.fields.field import Field
from hare.sql.functions.datetime.timestamp_comparand import TimestampComparand
from hare.sql.terms.array import Array
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.parameters.parameterized_value_wrapper import ParameterizedValueWrapper
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterator

    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.expressions.value_references.value_reference_types import ParameterValues


@dataclass(frozen=True)
class RebuiltCriterionValueReference:
    """A plan value reference of a lookup building its criterion from the value in a way of its own -
    a JSON ``__filter``, ``__has_keys`` (``FieldLookup.binds_by_rebuild``). A later value builds the
    criterion again - the plan key holds the value's shape, so the SQL text is the same - and the
    values of its terms take the places of the recorded criterion's, matched in order; a value the
    same in both stays. A term binding a value is a ``ValueWrapper`` or an ``Array`` bound whole as
    one parameter - whichever of them the text holds binds.
    """

    criterion: Criterion
    operator: Callable[..., Criterion]
    term: Term
    field: Field[Any]
    value_encoder: Callable[..., Any] | None = None

    def get_parameter_values(self, value: Any, model: type[Model], dialect: Dialect) -> ParameterValues | None:
        """The parameters of the criterion a new value builds.

        Args:
            value: The new value.
            model: The model queried.
            dialect: The dialect the query runs on.

        Returns:
            The parameters, or None when the criterion built differs in its terms - another number
            of values, or a value the SQL text holds itself changed - or the value is refused.
        """
        try:
            if self.value_encoder is not None:
                encoded_value = self.value_encoder(value, model, self.field, dialect)
            else:
                encoded_value = dialect.types.get_lookup_value(self.field, value, model)
            if isinstance(encoded_value, TimestampComparand):
                return None
            rebuilt = self.operator(self.term, encoded_value)
        except (HareError, ValueError, TypeError):
            # The full build reports what is wrong with the value.
            return None
        recorded_terms = self.get_value_terms(self.criterion)
        rebuilt_terms = self.get_value_terms(rebuilt)
        if len(recorded_terms) != len(rebuilt_terms):
            return None
        parameters: ParameterValues = []
        for (recorded, recorded_value), (rebuilt_term, rebuilt_value) in zip(
            recorded_terms, rebuilt_terms, strict=True
        ):
            if type(recorded_value) is type(rebuilt_value) and recorded_value == rebuilt_value:
                continue
            parameters.append((id(recorded), rebuilt_value, isinstance(rebuilt_term, ParameterizedValueWrapper)))
        return parameters

    @staticmethod
    def get_value_terms(criterion: Criterion) -> list[tuple[Term, Any]]:
        """The terms of a criterion that may bind a value, with the value each binds, in tree order.

        Args:
            criterion: The criterion.

        Returns:
            The terms and their values.
        """
        value_terms: list[tuple[Term, Any]] = []
        nodes: Iterator[Any] = criterion.nodes_()
        for node in nodes:
            if isinstance(node, ValueWrapper):
                value_terms.append((node, node.value))
            elif isinstance(node, Array):
                value_terms.append((node, node.original_value))
        return value_terms
