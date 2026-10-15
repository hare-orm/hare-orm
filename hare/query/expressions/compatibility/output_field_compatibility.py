from __future__ import annotations

from datetime import date, datetime
from typing import Any

from hare.exceptions import FieldError
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.query.expressions.compatibility.constants import (
    COALESCE_NUMERIC_FIELD_DEFAULT_FIELD_CLASSES,
    RESULT_VALUE_TYPE_FIELD_CLASSES,
    RESULT_VALUE_TYPE_LITERAL_TYPES,
)
from hare.query.expressions.compatibility.result_branch import ResultBranch
from hare.query.expressions.constants import COALESCE_NUMERIC_FIELD_LITERAL_TYPES
from hare.query.expressions.expression import Expression
from hare.query.expressions.numeric.numeric_type import NumericType
from hare.query.expressions.numeric.numeric_typing import NumericTyping
from hare.query.expressions.temporal.temporal_arithmetic import TemporalArithmetic
from hare.query.expressions.value import Value
from hare.sql.terms.term import Term


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
            output_field = cls.get_moment_output_field(typed_branches) or field_branches[0].field
            if output_field is not None and all(cls.is_branch_compatible(output_field, branch) for branch in branches):
                return output_field
            return None
        return cls.get_literal_output_field([branch.value for branch in typed_branches])

    @classmethod
    def get_moment_output_field(cls, typed_branches: list[ResultBranch]) -> Field[Any] | None:
        """The field of dates mixed with moments - a moment's, as PostgreSQL makes a date beside a
        timestamp a timestamp: decoded as a date, a moment would lose its time.

        Args:
            typed_branches: The branches of a known type.

        Returns:
            The field, None unless every branch is a date or a moment and one is a moment.
        """
        if any(cls.get_value_type(branch) != "a date or a moment" for branch in typed_branches):
            return None
        for branch in typed_branches:
            if branch.field is None and isinstance(branch.value, datetime):
                return TemporalArithmetic.DATETIME_OUTPUT_FIELD  # type: ignore[arg-type]
            if branch.field is not None and isinstance(
                GeneratedField.get_effective_field(branch.field), DatetimeField
            ):
                return branch.field
        return None

    @classmethod
    def raise_if_value_types_mix(
        cls, branches: list[ResultBranch], expression_name: str, verb: str = "combines"
    ) -> None:
        """Refuses branches holding values of types that don't combine - a number and text, a
        boolean and a number, ... - as PostgreSQL refuses them on every backend.

        Args:
            branches: Every value the result can be picked from.
            expression_name: Names the expression in the error.
            verb: What the expression does with the values, in the error.

        Raises:
            FieldError: Two branches hold values of such types.
        """
        cls.raise_if_types_differ(list(map(cls.get_value_type, branches)), expression_name, verb)

    @staticmethod
    def raise_if_types_differ(value_types: list[str | None], expression_name: str, verb: str) -> None:
        """Refuses values of types (``RESULT_VALUE_TYPE_FIELD_CLASSES``) that don't combine.

        Args:
            value_types: The type of each value, None for a value of no type there.
            expression_name: Names the expression in the error.
            verb: What the expression does with the values, in the error.

        Raises:
            FieldError: Two values are of such types.
        """
        value_types = list(dict.fromkeys(filter(None, value_types)))
        if len(value_types) > 1:
            raise FieldError(
                f"{expression_name} {verb} {' and '.join(value_types)}, which don't make one type - "
                "convert them to one with Cast()"
            )

    @staticmethod
    def get_python_value_type(python_type: Any) -> str | None:
        """The type of the values of a Python type, as ``RESULT_VALUE_TYPE_LITERAL_TYPES`` names it.

        Args:
            python_type: The Python type.

        Returns:
            The name of the type, None for a type of no type there.
        """
        if not isinstance(python_type, type):
            return None
        for value_type, literal_types in RESULT_VALUE_TYPE_LITERAL_TYPES:
            if issubclass(python_type, literal_types):
                return value_type
        return None

    @staticmethod
    def get_value_type(branch: ResultBranch) -> str | None:
        """The type of the values of a branch, as ``RESULT_VALUE_TYPE_FIELD_CLASSES`` names it.

        Args:
            branch: The branch.

        Returns:
            The name of the type, None for a branch of no type there or of no known type.
        """
        if not branch.is_typed:
            return None
        if branch.field is None:
            for value_type, literal_types in RESULT_VALUE_TYPE_LITERAL_TYPES:
                if isinstance(branch.value, literal_types):
                    return value_type
            return None
        if NumericTyping.get_field_type(branch.field) is not None:
            return "a number"
        effective_field = GeneratedField.get_effective_field(branch.field)
        for value_type, field_classes in RESULT_VALUE_TYPE_FIELD_CLASSES:
            if isinstance(effective_field, field_classes):
                return value_type
        return None

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
