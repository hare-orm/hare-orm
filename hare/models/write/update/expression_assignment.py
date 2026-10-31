from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, QueryError
from hare.fields.data.choices.int_enum_field_instance import IntEnumFieldInstance
from hare.fields.data.containers.array_field import ArrayField
from hare.fields.data.numeric.int_field import IntField
from hare.fields.encrypted.encrypted_field_base import EncryptedFieldBase
from hare.fields.generated_field import GeneratedField
from hare.query.expressions import Expression, Value
from hare.query.expressions.compatibility.constants import RANGE_VALUE_TYPE
from hare.query.expressions.compatibility.output_field_compatibility import OutputFieldCompatibility
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.constants import PLAIN_VALUE_TYPES
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.term_expression import TermExpression
from hare.query.filters.resolution.filter_values import FilterValues
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
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
            # Value(None) written is the NULL a plain None writes - only a filter compares with it.
            return None if value.value is None else FilterValues.get_literal_value(value)
        if isinstance(value, Term) and not isinstance(value, Expression):
            return TermExpression(value)
        return value

    @staticmethod
    def raise_if_literal_refused(field_name: str, field: Field[Any], expression: Expression, dialect: Dialect) -> None:
        """Refuses a literal the expression may set the field to (``Expression.get_result_literals()``)
        which the field refuses as a plain value - a number of no member of an enum, a text past
        ``max_length``, a number whose text is past it. A NULL is the column's to refuse.

        Args:
            field_name: The field's name.
            field: The field set.
            expression: The expression.
            dialect: The dialect the update runs on.

        Raises:
            FieldError: A moment set into a wall clock in no zone.
            ValidationError: The field refuses the literal.
        """
        field_branch = ResultBranch(None, field, is_literal=False)
        for literal in expression.get_result_literals():
            if literal is None:
                continue
            OutputFieldCompatibility.raise_if_wall_clocks_mix_with_moments(
                [field_branch, ResultBranch(literal, None, is_literal=True)], f"update({field_name}=...)", "sets"
            )
            dialect.types.get_db_value(field, literal, None)

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
        ExpressionAssignment.raise_if_literal_refused(field_name, field, expression, context.dialect)
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
        TemporalArithmetic.raise_if_not_assignable(field_name, field, result.output_field)
        ExpressionAssignment.raise_if_type_not_assignable(field_name, field, result.output_field)
        if result.joins:
            # The SET clause can't read a joined table without a dialect's own cross-table UPDATE
            # syntax, which isn't generated.
            raise QueryError(
                f"Field {field_name!r} can't be updated to an expression that references a related model's "
                "field (e.g. F('relation__field')) - .update()'s SET clause can't reference a joined table. "
                "Fetch the value yourself and pass it as a plain value instead, or use "
                ".filter(...).values_list(...) plus a per-row .update() / bulk_update()."
            )
        value_field = result.output_field
        term = result.term
        if (
            isinstance(field, IntField)
            and FunctionArguments.get_cast_type(value_field) in FunctionArguments.FRACTIONAL_CAST_TYPES
        ):
            # Converted where a database would keep the fraction in the column.
            term = context.dialect.renderers.get_assigned_integer_term(term)
        if isinstance(field, IntEnumFieldInstance):
            return context.dialect.renderers.get_assigned_enum_term(
                term, [int(member.value) for member in field.enum_type]
            )
        return term

    @staticmethod
    def raise_if_type_not_assignable(field_name: str, field: Field[Any], value_field: Field[Any] | None) -> None:
        """Refuses setting a field to an expression of a type it doesn't take - text into a number, a
        number into a boolean, ...: a database keeping the value as it is would leave a row the
        field reads back none of. A text field takes any value, as its text.

        Args:
            field_name: The field's name.
            field: The field.
            value_field: The field the expression's value is decoded through.

        Raises:
            FieldError: The field and the expression hold values of such types.
        """
        field_branch = ResultBranch(None, field, is_literal=False)
        field_value_type = OutputFieldCompatibility.get_value_type(field_branch)
        if value_field is None or field_value_type == "text":
            return
        expression_name = f"update({field_name}=...)"
        if field_value_type is not None:
            OutputFieldCompatibility.raise_if_value_types_mix(
                [field_branch, ResultBranch(None, value_field, is_literal=False)], expression_name, "sets"
            )
        # The types of values only fields of their own family store - bytes, JSON, an array, ...
        OutputFieldCompatibility.raise_if_types_differ(
            [
                ExpressionAssignment.get_stored_value_type(field),
                ExpressionAssignment.get_stored_value_type(value_field),
            ],
            expression_name,
            "sets",
        )

    @staticmethod
    def get_stored_value_type(field: Field[Any]) -> str | None:
        """The type of the values a field stores - its family's own (``Field.argument_value_type``), an
        array's with its elements' and a range's with its column type, else the type a CASE combines it
        by.

        Args:
            field: The field.

        Returns:
            The name of the type, None for a field of no known type.
        """
        effective_field = GeneratedField.get_effective_field(field)
        family_value_type = effective_field.argument_value_type
        if family_value_type is None:
            return OutputFieldCompatibility.get_value_type(ResultBranch(None, field, is_literal=False))
        if isinstance(effective_field, ArrayField):
            # An array takes another one whose elements its elements take - no text from numbers.
            element_value_type = ExpressionAssignment.get_stored_value_type(effective_field.base_field)
            return f"{family_value_type} of {element_value_type}" if element_value_type else family_value_type
        if family_value_type == RANGE_VALUE_TYPE:
            # A range of other bounds is another type of the database.
            return f"{family_value_type} ({effective_field.SQL_TYPE})"
        return family_value_type

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
