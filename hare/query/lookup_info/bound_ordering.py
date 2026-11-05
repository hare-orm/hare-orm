from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.exceptions import UnSupportedError
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.generated_field import GeneratedField
from hare.sql.functions.json.json_sort_key import JsonSortKey
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.query.expressions.expression_context import ExpressionContext
    from hare.sql import Order
    from hare.sql.terms.term import Term


@dataclass(frozen=True, slots=True)
class BoundOrdering:
    """The value, comparison key and NULL placement of one scalar ordering.

    ORDER BY and cursor predicates use ``term``. Boundary values pass through the
    field's read-column codec and the same comparison-key transformation. A wrapper
    is allocated explicitly before that transformation so plan recording never has
    to recover parameters by inspecting the generated comparison tree.
    """

    source_term: Term
    term: Term
    field: Field[Any] | None
    direction: Order
    nulls_first: bool
    context: ExpressionContext

    @classmethod
    def bind(cls, context: ExpressionContext, term: Term, field: Field[Any] | None, direction: Order) -> BoundOrdering:
        """Bind an expression's ordering contract using effective connection features."""
        if field is not None:
            EncryptedFieldBase.raise_if_encrypted(field, "ORDER BY")
            effective_field = GeneratedField.get_effective_field(field)
            if not effective_field.is_orderable:
                raise UnSupportedError(
                    f"{field.model_field_name} is a {type(effective_field).__name__} - the database orders "
                    "no rows by it; order by a path of it"
                )
        features = context.connection.features if context.connection is not None else context.dialect.features
        return cls(
            term,
            cls.get_comparison_key(context, field, term),
            field,
            direction,
            direction.puts_nulls_first(features.sorts_nulls_first),
            context,
        )

    @staticmethod
    def get_comparison_key(context: ExpressionContext, field: Field[Any] | None, term: Term) -> Term:
        """Apply the field's SQL ordering representation to either side of a comparison."""
        if field is None:
            return term
        if isinstance(GeneratedField.get_effective_field(field), JSONField):
            term = JsonSortKey(term)
        return context.dialect.types.get_ordering_key(field, term)

    def encode_value(self, value: Any) -> Any:
        """Convert a read cursor value to storage form; a missing joined row stays NULL."""
        if value is None or self.field is None:
            return value
        return self.context.dialect.types.get_read_column_value(self.field, value, self.context.model)

    def get_boundary_term(self, encoded_value: Any) -> tuple[Term, ValueWrapper]:
        """Return the comparison key and its explicit source for parameter recording."""
        wrapper = ValueWrapper(encoded_value)
        if self.field is not None and self.field.sensitive:
            wrapper.sensitive = True
        return self.get_comparison_key(self.context, self.field, wrapper), wrapper
