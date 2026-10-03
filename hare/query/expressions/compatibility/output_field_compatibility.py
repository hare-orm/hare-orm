from __future__ import annotations

from datetime import date, datetime
from typing import Any

from hare.fields.base.field import Field
from hare.query.expressions.base.expression import Expression
from hare.query.expressions.base.value import Value
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.constants import (
    COALESCE_NUMERIC_FIELD_DEFAULT_FIELD_CLASSES,
    COALESCE_NUMERIC_FIELD_LITERAL_TYPES,
)
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.sql.terms.base.term import Term


class OutputFieldCompatibility:
    """The field a value picked from several alternatives (a COALESCE default, a CASE branch) is
    decoded through."""

    @staticmethod
    def is_value_compatible(output_field: Field[Any], value: Any, value_field: Field[Any] | None) -> bool:
        """Whether one alternative value fits ``output_field``. Ruled out only for a numeric field
        given a value wider than it holds - a literal, or an expression resolving to another field.

        Args:
            output_field: The field the result would be decoded through.
            value: The original value argument.
            value_field: The field the resolved value is decoded through.

        Returns:
            False when the field's type can't represent the value.
        """
        if isinstance(value, Value):
            value = value.value
        return OutputFieldCompatibility.is_branch_compatible(
            output_field, ResultBranch(value, value_field, is_literal=not isinstance(value, (Expression, Term)))
        )

    @staticmethod
    def is_branch_compatible(output_field: Field[Any], branch: ResultBranch) -> bool:
        """Whether one CASE/COALESCE branch is type-compatible with `output_field`.

        Args:
            output_field: The field the result would be decoded through.
            branch: The branch.

        Returns:
            False when the branch's value can't be represented by the field's own type.
        """
        value = branch.value
        value_field = branch.field
        for numeric_field_class, literal_types in COALESCE_NUMERIC_FIELD_LITERAL_TYPES.items():
            if not isinstance(output_field, numeric_field_class):
                continue
            if not branch.is_literal:
                return value_field is None or isinstance(
                    value_field, COALESCE_NUMERIC_FIELD_DEFAULT_FIELD_CLASSES[numeric_field_class]
                )
            return value is None or isinstance(value, literal_types)
        return True

    @classmethod
    def get_common_output_field(cls, branches: list[ResultBranch]) -> Field[Any] | None:
        """The field a value picked from ``branches`` is decoded through. Numbers of different types
        give a float when one is a float, else a Decimal of the largest scale when one is a Decimal,
        else an integer. Otherwise the first branch's field when every other branch fits it;
        literals alone take their common type. A None literal or an expression of unknown type
        doesn't take part.

        Args:
            branches: Every value the result can be picked from.

        Returns:
            The field, None (the raw driver value) when the branches have no common type.
        """
        typed_branches = [branch for branch in branches if branch.is_typed]
        if not typed_branches:
            return None
        numeric_types: list[NumericType] = []
        for branch in typed_branches:
            numeric_type = (
                NumericTyping.get_field_type(branch.field)
                if branch.field is not None
                else NumericTyping.get_literal_type(branch.value)
            )
            if numeric_type is None:
                break
            numeric_types.append(numeric_type)
        else:
            fields = [branch.field for branch in typed_branches if branch.field is not None]
            return NumericTyping.get_common_output_field(numeric_types, fields)
        field_branches = [branch for branch in typed_branches if branch.field is not None]
        if field_branches:
            output_field = field_branches[0].field
            if output_field is not None and all(cls.is_branch_compatible(output_field, branch) for branch in branches):
                return output_field
            return None
        return cls.get_literal_output_field([branch.value for branch in typed_branches])

    @staticmethod
    def get_literal_output_field(literal_values: list[Any]) -> Field[Any] | None:
        """The field a result picked from non-numeric literals is decoded through.

        Args:
            literal_values: The literals, None aside.

        Returns:
            The literals' own field when they all have one type, a datetime field for dates mixed
            with datetimes, else None.
        """
        literal_types = {type(value) for value in literal_values}
        if literal_types == {date, datetime}:
            return TemporalArithmetic.DATETIME_OUTPUT_FIELD  # type: ignore[arg-type]
        if len(literal_types) != 1:
            return None
        return Value.get_literal_output_field(literal_values[0])
