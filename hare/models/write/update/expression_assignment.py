from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, QueryError
from hare.fields.data.numeric.decimal_field import DecimalField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.query.expressions import CombinedExpression, Expression, Value
from hare.query.expressions.constants import PLAIN_VALUE_TYPES
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.term_expression import TermExpression
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.term import Term

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.query.expressions import ExpressionContext


class ExpressionAssignment:
    """An UPDATE setting a field from an expression - ``save()`` of an instance holding one,
    and ``update()`` - resolved and checked the same way."""

    @staticmethod
    def get_assigned_value(value: Any) -> Any:
        """A value assigned to a column, as the statement writes it: a SQL term (``RawSQL(...)``, a
        ``hare.sql`` term) as an expression embedding it, a literal (``Value(...)``) as its plain
        value - converted and validated by the field the way the same plain value is.

        Args:
            value: The value given.

        Returns:
            The value written.
        """
        value_type = type(value)
        if value_type in PLAIN_VALUE_TYPES:
            return value
        if value_type is Value:
            return FilterValues.get_literal_value(value)
        if isinstance(value, Term) and not isinstance(value, Expression):
            return TermExpression(value)
        return value

    @staticmethod
    def get_term(field_name: str, field: Field[Any], expression: Expression, context: ExpressionContext) -> Term:
        """The SQL the field is set to.

        Args:
            field_name: The field's name.
            field: The field.
            expression: The expression.
            context: The context resolving it, on the updated table.

        Returns:
            The term.

        Raises:
            QueryError: The expression reads a window function, or a related model's field.
            FieldError: The expression is an aggregate, of another date/time type than the
                field, or can't be written into (or read out of) an encrypted field.
        """
        # Deferred import: hare.query.queryset imports the writes.

        result = expression.get_result(context)
        may_aggregate = ExpressionAssignment._has_window_aggregate_or_subquery(result.term)
        if may_aggregate and RowMultiplication.term_reads_window_function(result.term):
            raise QueryError(
                f"Field {field_name!r} can't be updated to a window function (Window(...)) - SQL does not "
                "allow window functions in UPDATE. Compute the values with .values_list(...) and write them "
                "with bulk_update() instead."
            )
        if may_aggregate and result.term.contains_aggregate:
            raise FieldError(
                f"Field {field_name!r} can't be updated to an aggregate (Count/Sum/Max/...) or an aggregate "
                "annotation - an UPDATE has no groups to aggregate over. Compute the value in a "
                "Subquery(...) over the related rows instead, or fetch it with .values_list(...) and write "
                "it with bulk_update()."
            )
        EncryptedFieldBase.raise_if_update_expression_invalid(field, expression, expression.get_value_field(result))
        if isinstance(expression, CombinedExpression):
            TemporalArithmetic.raise_if_not_assignable(field_name, field, result.output_field)  # type: ignore[call-overload]
        if result.joins:
            # The SET clause can't read a joined table without a dialect's own cross-table UPDATE
            # syntax, which isn't generated.
            raise QueryError(
                f"Field {field_name!r} can't be updated to an expression that references a related model's "
                "field (e.g. F('relation__field')) - .update()'s SET clause can't reference a joined table. "
                "Fetch the value yourself and pass it as a plain value instead, or use "
                ".filter(...).values_list(...) plus a per-row .update() / bulk_update()."
            )
        if isinstance(field, DecimalField):
            return context.dialect.renderers.get_assigned_decimal_term(
                result.term, field.max_digits, field.decimal_places
            )
        return result.term

    @staticmethod
    def _has_window_aggregate_or_subquery(term: Term) -> bool:
        """Whether any node of ``term`` is a window function, an aggregate or a subquery - only then
        can it read a window function or an aggregate.

        Args:
            term: The term.

        Returns:
            True when such a node appears anywhere in the term.
        """
        nodes: Iterator[Term] = term.nodes_()
        return any(node.is_analytic or node.is_subquery or isinstance(node, AggregateFunction) for node in nodes)
